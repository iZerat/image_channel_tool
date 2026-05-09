@echo off
chcp 65001 >nul
title Image Channel Tool - Backend Launcher
echo ============================================
echo   Image Channel Tool - Backend Launcher
echo ============================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.8+ from https://python.org
    pause
    exit /b 1
)

echo [OK] Python detected.
python --version
echo.

REM Check if pip is available
pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip is not available.
    pause
    exit /b 1
)

echo [OK] pip detected.
echo.

REM Install dependencies if requirements.txt exists
if exist "requirements.txt" (
    echo [INFO] Installing / checking dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [WARNING] Some dependencies may have failed to install.
        echo [WARNING] If EXR/HDR support is needed, install FreeImage manually.
    )
    echo.
) else (
    echo [WARNING] requirements.txt not found. Skipping dependency check.
    echo.
)

echo [INFO] Starting Flask backend...
echo [INFO] API will be available at http://localhost:5000
echo [INFO] Press Ctrl+C to stop the server.
echo.

REM Start the backend in a new window so the script can continue
echo [INFO] Launching backend server...
start "Image Channel Backend" cmd /k "python app.py"

REM Wait a moment for the server to start
timeout /t 3 /nobreak >nul

echo.
echo [INFO] Opening image_channel_py.html in browser...
echo.

REM Open the HTML file
if exist "image_channel_py.html" (
    start "" "image_channel_py.html"
) else (
    echo [WARNING] image_channel_py.html not found in current directory.
    echo Please navigate to the correct folder.
)

echo ============================================
echo   Backend is running. Browser should open.
echo   Close this window when done.
echo ============================================
pause