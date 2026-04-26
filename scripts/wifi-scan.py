#!/usr/bin/env python3
import subprocess
import re
import json
import sys
import shutil

def get_wifi_networks():
    wifi_networks = []
    
    # Check if nmcli is available
    # Removed nmcli code block as it was incomplete and iwlist is preferred for this device.
    # if shutil.which("nmcli"): ... 
    try:
        # Determine iwlist path
        iwlist_path = "iwlist"
        if shutil.which("/sbin/iwlist"):
            iwlist_path = "/sbin/iwlist"
        elif shutil.which("/usr/sbin/iwlist"):
            iwlist_path = "/usr/sbin/iwlist"
            
        cmd = ["sudo", iwlist_path, "wlan0", "scan"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            # Try just 'iwlist scan' (might find other interfaces)
            cmd = ["sudo", iwlist_path, "scan"]

            result = subprocess.run(cmd, capture_output=True, text=True)
            
        if result.returncode == 0:
            content = result.stdout
            networks = []
            current_network = {}
            
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("Cell"):
                    if current_network:
                        networks.append(current_network)
                    current_network = {}
                    # Cell 01 - Address: XX:XX...
                    match = re.search(r"Address:\s*([0-9A-F:]{17})", line, re.IGNORECASE)
                    if match:
                        current_network['mac'] = match.group(1)
                elif line.startswith("ESSID:"):
                    ssid = line.split(":", 1)[1].strip('"')
                    current_network['ssid'] = ssid
                elif line.startswith("Channel:"):
                    try:
                        current_network['channel'] = int(line.split(":")[1])
                    except ValueError:
                        pass
                elif line.startswith("Frequency:"):
                    # Frequency:2.417 GHz (Channel 2)
                    match = re.search(r"Frequency:([\d\.]+)\s*GHz", line)
                    if match:
                        current_network['frequency'] = float(match.group(1))
                elif "Signal level" in line:
                    # Quality=XX/XX Signal level=-XX dBm
                    match = re.search(r"Signal level=([-\d]+)", line)
                    if match:
                        current_network['signal_strength'] = int(match.group(1))
                    
                    match_quality = re.search(r"Quality=([\d]+)/([\d]+)", line)
                    if match_quality:
                        current_network['quality'] = f"{match_quality.group(1)}/{match_quality.group(2)}"
                elif line.startswith("Encryption key:"):
                    current_network['encrypted'] = (line.split(":")[1] == "on")
                
            if current_network:
                networks.append(current_network)
            
            return networks
            
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
        
    return []

if __name__ == "__main__":
    try:
        networks = get_wifi_networks()
        print(json.dumps(networks, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
