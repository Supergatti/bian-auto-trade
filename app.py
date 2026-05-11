from config import logger, MODEL_PRO, BINANCE_API_KEY, BINANCE_SECRET_KEY, ACCESS_TOKEN
from flask import Flask, render_template

app = Flask(__name__)

from utils.auth import check_auth

app.before_request(check_auth)

from routes.account_market import account_bp, market_bp
from routes.favorites import fav_bp
from routes.trade import trade_bp
from routes.manual_trade import manual_bp

app.register_blueprint(account_bp)
app.register_blueprint(market_bp)
app.register_blueprint(fav_bp)
app.register_blueprint(trade_bp)
app.register_blueprint(manual_bp)


@app.route("/")
def index():
    return render_template("index.html", access_token=ACCESS_TOKEN)


if __name__ == "__main__":
    logger.info("🟢 币安自动交易系统启动...")
    logger.info("   Pro 模型: %s  |  Flash 模型: deepseek-v4-flash", MODEL_PRO)
    logger.info("   API Key: %s  |  Secret Key: %s",
                "是" if BINANCE_API_KEY else "否",
                "是" if BINANCE_SECRET_KEY else "否")
    logger.info("   访问认证: %s", "已启用" if ACCESS_TOKEN else "⚠ 未设置 (公开访问)")
    logger.info("   监听地址: http://127.0.0.1:8080")
    app.run(debug=True, host="127.0.0.1", port=8080, use_reloader=False)
