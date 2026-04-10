import asyncio
import uuid
import time
import re
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

class JobStatus(str, Enum):
    STARTED = "started"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

@dataclass
class Job:
    id: str
    command: str
    status: JobStatus = JobStatus.STARTED
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    logs: List[str] = field(default_factory=list)
    # Queues for active websocket listeners
    listeners: List[asyncio.Queue] = field(default_factory=list)

    async def add_log(self, line: str):
        self.logs.append(line)
        # Broadcast to all active listeners
        for listener in self.listeners:
            await listener.put(line)

    def register_listener(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self.listeners.append(q)
        return q

    def remove_listener(self, q: asyncio.Queue):
        if q in self.listeners:
            self.listeners.remove(q)

class JobManager:
    def __init__(self):
        self.jobs: Dict[str, Job] = {}

    @staticmethod
    def _sanitize_log_line(line: str) -> str:
        """Redact common credential tokens before storing/streaming logs."""
        redacted = re.sub(r'(\bpassword\s+)("[^"]*"|\S+)', r'\1***', line, flags=re.IGNORECASE)
        redacted = re.sub(r'(\bwifi-sec\.psk\s+)("[^"]*"|\S+)', r'\1***', redacted, flags=re.IGNORECASE)
        return redacted

    async def start_job(
        self,
        command: List[str],
        job_id: Optional[str] = None,
        display_command: Optional[str] = None,
    ) -> str:
        if not job_id:
            job_id = str(uuid.uuid4())
        job = Job(id=job_id, command=display_command if display_command is not None else " ".join(command))
        self.jobs[job_id] = job
        
        # Start background task to run the process
        asyncio.create_task(self._run_process(job_id, command))
        
        return job_id

    async def _monitor_detached_unit(self, job: Job, unit_name: str):
        await job.add_log(f"Monitoring detached unit: {unit_name}")
        
        # Start journalctl to follow logs
        journal_cmd = ["sudo", "journalctl", "-f", "-u", unit_name, "--no-tail"]
        journal_proc = await asyncio.create_subprocess_exec(
            *journal_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        async def read_journal():
            while True:
                line = await journal_proc.stdout.readline()
                if not line:
                    break
                decoded = self._sanitize_log_line(line.decode(errors='replace').strip())
                if decoded:
                    await job.add_log(decoded)

        # Start reading logs in background
        log_task = asyncio.create_task(read_journal())

        # Monitor service status
        final_status = "unknown"
        exit_code = 0
        
        while True:
            await asyncio.sleep(2)
            
            # Check if active
            check_cmd = ["sudo", "systemctl", "is-active", unit_name]
            check_proc = await asyncio.create_subprocess_exec(
                *check_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await check_proc.communicate()
            status = stdout.decode().strip()
            
            if status in ["inactive", "failed"]:
                # Service finished
                final_status = status
                break
        
        # Stop logging
        if journal_proc.returncode is None:
            journal_proc.terminate()
            try:
                await asyncio.wait_for(journal_proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                journal_proc.kill()
        
        await log_task

        # Get detailed exit status. Transient units can disappear quickly,
        # so failure to read metadata should not force a failed job result.
        result = "unknown"
        show_stdout = b""
        show_stderr = b""
        show_cmd = ["sudo", "-n", "systemctl", "show", "-p", "ExecMainStatus,Result", "--value", unit_name]
        try:
            show_proc = await asyncio.create_subprocess_exec(
                *show_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            show_stdout, show_stderr = await show_proc.communicate()
            if show_proc.returncode == 0:
                lines = [line.strip() for line in show_stdout.decode(errors='replace').splitlines() if line.strip()]
                if lines:
                    try:
                        exit_code = int(lines[0])
                    except ValueError:
                        exit_code = 0
                    if len(lines) > 1:
                        result = lines[1]
            else:
                await job.add_log(
                    f"Warning: could not read final unit status for {unit_name}: "
                    f"{show_stderr.decode(errors='replace').strip() or 'unknown error'}"
                )
        except Exception as e:
            await job.add_log(f"Warning: error while reading final unit status for {unit_name}: {e!r}")
            
        job.exit_code = exit_code
        job.finished_at = time.time()
        
        # Check success conditions:
        # 1. Systemd reports success (clean exit 0)
        # 2. Unit is inactive with exit code 0 (systemd Result can be flaky for transient units)
        # 3. Script emitted explicit success markers in logs.
        is_success = (
            (final_status == "inactive" and result == "success" and exit_code == 0)
            or (final_status == "inactive" and exit_code == 0 and result in {"unknown", "", "success", "failed"})
        )

        log_success = any(
            "System upgrade completed successfully." in log
            or "System is already up to date." in log
            for log in job.logs
        )

        if not is_success and log_success:
            is_success = True
            job.exit_code = 0  # Explicit success marker from script output.

        # Keep status and exit code coherent for clients.
        if is_success and job.exit_code != 0:
            job.exit_code = 0
        if not is_success and job.exit_code == 0:
            job.exit_code = 1

        await job.add_log(
            f"Final unit evaluation: is-active={final_status}, result={result}, exit_code={job.exit_code}"
        )
        
        job.status = JobStatus.SUCCESS if is_success else JobStatus.FAILED
        
        for listener in job.listeners:
            await listener.put(None)

    async def _run_process(self, job_id: str, command: List[str]):
        job = self.jobs.get(job_id)
        if not job:
            return

        job.status = JobStatus.RUNNING
        detached_unit = None
        
        try:
            # Create subprocess
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT 
            )

            # Read output line by line
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                # robust decoding used
                decoded_line = self._sanitize_log_line(line.decode(errors='replace').strip())
                if decoded_line: 
                    await job.add_log(decoded_line)
                    # Check for detachment
                    if "Running as unit:" in decoded_line:
                        # Extract unit name, e.g. "Running as unit: pins-sysupgrade-123.service"
                        parts = decoded_line.split("Running as unit:")
                        if len(parts) > 1:
                            detached_unit = parts[1].strip()

            await process.wait()
            
            # If process exited successfully and we detected a detached unit, switch to monitoring it
            if process.returncode == 0 and detached_unit:
                await self._monitor_detached_unit(job, detached_unit)
                return

            job.exit_code = process.returncode
            job.finished_at = time.time()
            job.status = JobStatus.SUCCESS if job.exit_code == 0 else JobStatus.FAILED
            
            # Notify listeners that job is done
            for listener in job.listeners:
                await listener.put(None)

        except Exception as e:
            import traceback
            error_msg = f"Internal Error: {repr(e)}"
            print(f"Job failed with exception: {traceback.format_exc()}") # Print to server console for debugging
            await job.add_log(error_msg)
            
            job.exit_code = -1
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            for listener in job.listeners:
                await listener.put(None)

    def get_job(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def get_latest_job(self) -> Optional[Job]:
        if not self.jobs:
            return None
        return max(self.jobs.values(), key=lambda job: job.created_at)

# Global instance
job_manager = JobManager()
