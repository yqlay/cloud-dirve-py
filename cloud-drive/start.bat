@echo off
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo Starting Cloud Drive...
python app.py
pause
