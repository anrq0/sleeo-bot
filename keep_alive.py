import os, logging
from threading import Thread
from flask import Flask

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

@app.route("/")
def home():
    return "Le bot est en ligne !"

def run():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

def keep_alive():
    Thread(target=run, daemon=True).start()
