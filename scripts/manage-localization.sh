#!/bin/sh
set -eu

export LC_ALL=C
export LANG=C

LOCALE_VALUE=""
WIFI_COUNTRY_VALUE=""
TIMEZONE_VALUE=""
KEYBOARD_LAYOUT_VALUE=""
HAS_VALUE=0

fail() {
    printf 'Localization error: %s\n' "$1" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --locale)
            [ "$#" -ge 2 ] || fail "--locale requires a value"
            LOCALE_VALUE=$2
            HAS_VALUE=1
            shift 2
            ;;
        --wifi-country)
            [ "$#" -ge 2 ] || fail "--wifi-country requires a value"
            WIFI_COUNTRY_VALUE=$2
            HAS_VALUE=1
            shift 2
            ;;
        --timezone)
            [ "$#" -ge 2 ] || fail "--timezone requires a value"
            TIMEZONE_VALUE=$2
            HAS_VALUE=1
            shift 2
            ;;
        --keyboard-layout)
            [ "$#" -ge 2 ] || fail "--keyboard-layout requires a value"
            KEYBOARD_LAYOUT_VALUE=$2
            HAS_VALUE=1
            shift 2
            ;;
        *)
            fail "unsupported argument: $1"
            ;;
    esac
done

[ "$HAS_VALUE" -eq 1 ] || fail "at least one localization value is required"

if [ -n "$LOCALE_VALUE" ]; then
    printf '%s' "$LOCALE_VALUE" | grep -Eq '^[A-Za-z0-9_.@-]+$' \
        || fail "invalid locale syntax"
    grep -Eq "^${LOCALE_VALUE}[[:space:]]+UTF-8([[:space:]]|$)" /usr/share/i18n/SUPPORTED \
        || fail "unsupported locale"
fi

if [ -n "$WIFI_COUNTRY_VALUE" ]; then
    printf '%s' "$WIFI_COUNTRY_VALUE" | grep -Eq '^[A-Z]{2}$' \
        || fail "invalid Wi-Fi country syntax"
    grep -Eq "^${WIFI_COUNTRY_VALUE}[[:space:]]" /usr/share/zoneinfo/iso3166.tab \
        || fail "unsupported Wi-Fi country"
fi

if [ -n "$TIMEZONE_VALUE" ]; then
    case "$TIMEZONE_VALUE" in
        /*|*..*|*[!A-Za-z0-9._+/-]*) fail "invalid timezone syntax" ;;
    esac
    [ -f "/usr/share/zoneinfo/$TIMEZONE_VALUE" ] || fail "unsupported timezone"
fi

if [ -n "$KEYBOARD_LAYOUT_VALUE" ]; then
    printf '%s' "$KEYBOARD_LAYOUT_VALUE" | grep -Eq '^[a-z0-9_-]+$' \
        || fail "invalid keyboard layout syntax"
    localectl list-x11-keymap-layouts | grep -Fxq "$KEYBOARD_LAYOUT_VALUE" \
        || fail "unsupported keyboard layout"
fi

if [ -n "$LOCALE_VALUE" ]; then
    CURRENT_LOCALE=$(sed -n 's/^LANG=["'\'']\{0,1\}\([^"'\'']*\)["'\'']\{0,1\}$/\1/p' /etc/default/locale | head -n 1)
    if [ "$CURRENT_LOCALE" = "$LOCALE_VALUE" ]; then
        printf 'Locale already set to %s\n' "$LOCALE_VALUE"
    else
        printf 'Setting locale to %s\n' "$LOCALE_VALUE"
        /usr/bin/raspi-config nonint do_change_locale "$LOCALE_VALUE"
    fi
fi

if [ -n "$TIMEZONE_VALUE" ]; then
    CURRENT_TIMEZONE=$(timedatectl show --property=Timezone --value 2>/dev/null || true)
    if [ "$CURRENT_TIMEZONE" = "$TIMEZONE_VALUE" ]; then
        printf 'Timezone already set to %s\n' "$TIMEZONE_VALUE"
    else
        printf 'Setting timezone to %s\n' "$TIMEZONE_VALUE"
        timedatectl set-timezone "$TIMEZONE_VALUE"
    fi
fi

if [ -n "$KEYBOARD_LAYOUT_VALUE" ]; then
    CURRENT_KEYBOARD=$(sed -n 's/^XKBLAYOUT=["'\'']\{0,1\}\([^"'\'']*\)["'\'']\{0,1\}$/\1/p' /etc/default/keyboard | head -n 1)
    if [ "$CURRENT_KEYBOARD" = "$KEYBOARD_LAYOUT_VALUE" ]; then
        printf 'Keyboard layout already set to %s\n' "$KEYBOARD_LAYOUT_VALUE"
    else
        printf 'Setting keyboard layout to %s\n' "$KEYBOARD_LAYOUT_VALUE"
        /usr/bin/raspi-config nonint do_configure_keyboard "$KEYBOARD_LAYOUT_VALUE"
    fi
fi

if [ -n "$WIFI_COUNTRY_VALUE" ]; then
    CURRENT_WIFI_COUNTRY=$(/usr/bin/raspi-config nonint get_wifi_country 2>/dev/null || true)
    if [ "$CURRENT_WIFI_COUNTRY" = "$WIFI_COUNTRY_VALUE" ]; then
        printf 'Wi-Fi country already set to %s\n' "$WIFI_COUNTRY_VALUE"
    else
        printf 'Setting Wi-Fi country to %s\n' "$WIFI_COUNTRY_VALUE"
        /usr/bin/raspi-config nonint do_wifi_country "$WIFI_COUNTRY_VALUE"
    fi
fi

printf 'System localization update completed successfully.\n'
