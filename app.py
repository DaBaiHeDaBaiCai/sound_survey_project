import os
import uuid
import random
import datetime
import io
import pandas as pd

from flask import Flask, render_template, request, redirect, url_for, session, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select

# -------------------- Flask app & DB config --------------------
app = Flask(__name__)
app.secret_key = "survey_secret_key"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "experiment.db")

app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy()
db.init_app(app)

# -------------------- 数据模型 --------------------
class Response(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.String(50))
    version = db.Column(db.String(10))
    stimulus_label = db.Column(db.String(10))
    person = db.Column(db.String(10))
    trial_index = db.Column(db.Integer)
    start_time = db.Column(db.String(30))
    end_time = db.Column(db.String(30))
    q1 = db.Column(db.Integer)
    q2 = db.Column(db.Integer)
    q3 = db.Column(db.Integer)
    q4 = db.Column(db.Integer)
    q5 = db.Column(db.Integer)

with app.app_context():
    db.create_all()

# -------------------- 首页 --------------------
@app.route("/")
def index():
    return render_template("index.html")

# -------------------- 实验开始 --------------------
@app.route("/start/<version>")
def start(version):
    # 仅允许 cn / jp
    version = (version or "").lower()
    if version not in ("cn", "jp"):
        return "Invalid version (use 'cn' or 'jp')", 400

    # 读取并按版本筛选
    df = pd.read_csv("stimuli_list.csv")
    sub = df[df["version"].str.lower() == version].copy()
    if sub.empty:
        return f"No stimuli found for version={version}", 500

    # 转 dict + 打乱 + 固定 index
    records = sub.to_dict(orient="records")
    random.shuffle(records)
    for i, s in enumerate(records):
        # 统一字段名，避免缺失
        s["stimulus_label"] = s.get("label") or s.get("stimulus_label") or f"item{i+1}"
        s["person"] = s.get("person", "")
        s["url"] = s.get("url", "")
        s["index"] = i  # ✅ 固定 index，模板直接使用

    # 会话初始化
    session["participant_id"] = str(uuid.uuid4())[:8]
    session["version"] = version
    session["stimuli_order"] = records
    session["current_index"] = 0

    return redirect(url_for("experiment"))

# -------------------- 实验主流程 --------------------
@app.route("/experiment", methods=["GET", "POST"])
def experiment():
    # 会话校验
    if "stimuli_order" not in session or "current_index" not in session:
        return redirect(url_for("index"))

    version = session.get("version", "cn")
    stimuli_order = session["stimuli_order"]
    idx = int(session.get("current_index", 0))

    # 提交一条答案
    if request.method == "POST":
        # 容错：trial_index 以服务器 idx 为主，表单为空或非法则回退为 idx
        try:
            form_trial_index = int(request.form.get("trial_index", idx))
        except Exception:
            form_trial_index = idx

        # 越界保护
        if not (0 <= idx < len(stimuli_order)):
            return redirect(url_for("thank_you"))

        stim = stimuli_order[idx]

        # 写入一条记录
        r = Response(
            participant_id=session.get("participant_id", ""),
            version=version,
            stimulus_label=stim.get("stimulus_label", ""),
            person=stim.get("person", ""),
            trial_index=form_trial_index,
            start_time=request.form.get("start_time", ""),
            end_time=datetime.datetime.utcnow().isoformat(),
            q1=int(request.form["q1"]),
            q2=int(request.form["q2"]),
            q3=int(request.form["q3"]),
            q4=int(request.form["q4"]),
            q5=int(request.form["q5"]),
        )
        db.session.add(r)
        db.session.commit()

        # 进入下一条或结束
        session["current_index"] = idx + 1
        if session["current_index"] >= len(stimuli_order):
            return redirect(url_for("thank_you"))
        return redirect(url_for("experiment"))

    # GET：渲染当前条
    if idx >= len(stimuli_order):
        return redirect(url_for("thank_you"))

    stim = stimuli_order[idx]
    start_time = datetime.datetime.utcnow().isoformat()

    # 只把模板需要的字段传过去，保证 key 存在
    tmpl_stim = {
        "url": stim.get("url", ""),
        "stimulus_label": stim.get("stimulus_label", ""),
        "person": stim.get("person", ""),
        "index": stim.get("index", idx),
    }

    return render_template(
        "experiment.html",
        stim=tmpl_stim,
        index=idx + 1,
        total=len(stimuli_order),
        version=version,
        start_time=start_time,
    )

# -------------------- 感谢页 --------------------
@app.route("/thank_you")
def thank_you():
    return render_template("thank_you.html")

# -------------------- 管理员登录 --------------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if request.form["username"] == "admin" and request.form["password"] == "123456":
            session["admin"] = True
            return redirect("/admin/panel")
        else:
            return "登录失败 / ログイン失敗", 403
    return render_template("admin.html")

# -------------------- 管理员控制面板 --------------------
@app.route("/admin/panel")
def admin_panel():
    if not session.get("admin"):
        return redirect("/admin")
    count = Response.query.count()
    return render_template("admin_panel.html", count=count)

@app.route("/admin/logout")
def logout():
    session.pop("admin", None)
    return redirect("/admin")

# -------------------- 导出 CSV --------------------
@app.route("/admin/export_csv")
def export_csv():
    if not session.get("admin"):
        return redirect("/admin")

    stmt = select(Response)
    rows = db.session.execute(stmt).scalars().all()

    records = []
    for r in rows:
        records.append({
            "participant_id": r.participant_id,
            "version": r.version,
            "stimulus_label": r.stimulus_label,
            "person": r.person,
            "trial_index": r.trial_index,
            "start_time": r.start_time,
            "end_time": r.end_time,
            "Q1_清晰": r.q1,
            "Q2_喜欢": r.q2,
            "Q3_亲切": r.q3,
            "Q4_违和": r.q4,
            "Q5_冷淡": r.q5,
        })

    csv_buffer = io.StringIO()
    df = pd.DataFrame(records)
    df.to_csv(csv_buffer, index=False, encoding="utf-8-sig")

    mem = io.BytesIO(csv_buffer.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="responses.csv")

# -------------------- 导出数据库 --------------------
@app.route("/admin/export_db")
def export_db():
    if not session.get("admin"):
        return redirect("/admin")
    return send_file(DB_PATH, as_attachment=True)

# -------------------- 启动 --------------------
if __name__ == "__main__":
    app.run(debug=True)
