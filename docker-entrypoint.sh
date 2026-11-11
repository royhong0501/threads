#!/usr/bin/env bash
set -e

if [ "$1" = "login" ]; then
    # 登入需要看得到的瀏覽器視窗：在虛擬螢幕上開啟，透過 noVNC 從主機的瀏覽器操作
    export DISPLAY=:99
    Xvfb :99 -screen 0 1366x1024x24 -nolisten tcp >/dev/null 2>&1 &
    for _ in $(seq 50); do
        [ -e /tmp/.X11-unix/X99 ] && break
        sleep 0.1
    done
    x11vnc -display :99 -localhost -forever -shared -nopw -quiet >/dev/null 2>&1 &
    websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
    echo "請用瀏覽器開啟以下網址完成登入："
    echo "  http://localhost:6080/vnc.html?autoconnect=1&resize=scale"
    echo
fi

exec python /app/logtail.py "$@"
