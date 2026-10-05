@echo off
title Set static IP on Ethernet (for direct Pi connection)
echo ============================================================
echo   Ethernet static IP for direct Raspberry Pi cable
echo ============================================================
echo.

net session >nul 2>&1
if errorlevel 1 goto :noadmin
echo [OK] Running as administrator
echo.

echo [1/3] Backing up current IP config to Desktop...
netsh interface ip dump > "%USERPROFILE%\Desktop\ip-config-backup.txt" 2>nul
echo       Saved: %USERPROFILE%\Desktop\ip-config-backup.txt
echo.

echo [2/3] Your adapters:
echo ---------------------------------------------------------------
netsh interface show interface
echo ---------------------------------------------------------------
echo.
echo     To set the static IP, open Command Prompt AS ADMIN and run:
echo.
echo       netsh interface ipv4 set address name="ADAPTER" static 192.168.137.50 255.255.255.0 none
echo.
echo     Replace ADAPTER with the Ethernet adapter name from the list
echo     above (it is the one WITHOUT WLAN / VirtualBox / Local Area).
echo     On your system it should be either  以太网  or  Ethernet.
echo.
echo     Note: the "none" at the end means NO default gateway (correct
echo           for a direct cable).
echo.

echo [3/3] Scanning 192.168.137.0/24 (works even without the static IP)...
echo ---------------------------------------------------------------
ping -n 1 -w 30 192.168.137.1 >nul 2>&1
for /L %%i in (2,1,254) do ping -n 1 -w 30 192.168.137.%%i >nul 2>&1
echo === Devices found on this segment ===
arp -a | findstr "192.168.137"
echo ---------------------------------------------------------------
echo.
echo     Pi Ethernet MAC starts with  e4:5f:01
echo     If no device appears, the Pi has no IP on eth0 (likely its
echo     eth0 is not configured for DHCP) - then a direct cable will
echo     not help and we need display or SD-card access.
echo.
pause
