# Run the Wanas Gallery chatbot locally.
#   .\run.ps1
$Python = "C:\Users\Fathy\anaconda3\envs\wanas\python.exe"
& $Python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
