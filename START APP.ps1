$python = "C:\Users\SerVer2\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$appDir = "C:\Users\SerVer2\Documents\Claude ai\autocount-receipt-bot"

Write-Host "Starting DE Penang Donation Receipts App..." -ForegroundColor Green

# Start Streamlit in background
$proc = Start-Process -FilePath $python -ArgumentList "-m streamlit run app.py --server.headless true" -WorkingDirectory $appDir -PassThru

Write-Host "Waiting for app to load (10 seconds)..." -ForegroundColor Yellow
Start-Sleep -Seconds 10

# Open browser
Start-Process "http://localhost:8501"

Write-Host "App is running! Do NOT close this window." -ForegroundColor Green
Write-Host "Press any key to STOP the app." -ForegroundColor Red
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

# Stop Streamlit when user presses a key
Stop-Process -Id $proc.Id -Force
