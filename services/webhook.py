# services/webhook.py

import os
from flask import Flask, request, abort, send_from_directory
import ngrok
from pathlib import Path

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == 'POST':
        print(request.get_json())
        return 'OK', 200
    else:
        abort(400)

@app.route('/webhook/<filename>')
def serve_verification(filename):
    static_dir = Path(__file__).parent.parent / 'static'
    print(f"Looking for file in: {static_dir}") 
    print(f"File exists: {os.path.exists(os.path.join(static_dir, filename))}")  # debug
    return send_from_directory(static_dir, filename)

if __name__ == '__main__':
    from dotenv import load_dotenv
    import os
    load_dotenv()
    
    listener = ngrok.forward(
        5000,
        authtoken=os.getenv("NGROK_AUTH_TOKEN"),
    )
    print(f"✅ Ngrok tunnel: {listener.url()}/webhook")
    app.run(port=5000)