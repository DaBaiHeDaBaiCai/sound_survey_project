import os
import uuid
import random
import datetime
import io
import pandas as pd

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_file, abort
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select, text

# -------------------- Flask app & DB config --------------------
app = Flask(__name__)

# 更安全：从环境变量读取，未设置时使用默认
app.secret_key = os.getenv("SECRET_KEY", "survey_secret_key")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "123456")

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
    stimulus_label = db.Column(db.String(50))
    person = db.Column(db.String(20))
    trial_index = db.Column(db.Integer)
    start_time = db.Column(db.String(30))
    end_time = db.Column(db.String(30))
    q1 = db.Column(db.Integer)
    q2 = db.Column(db.Integer)
    q3 = db.Column(db.Integer)
    q4 = db.Column(db.Integer)
    q5 = db.Column(db.Integer)

    run_id = db.Column(db.String(36))           # 同一次作答的 ID
    is_complete = db.Column(db.Boolean, default=False)  # 该次作答是否已完整提交

with app.app_context():
    db.create_all()

    # 软迁移：为旧库补充列
    from sqlalchemy import inspect, text as sa_text
    insp = inspect(db.engine)
    cols = [c["name"] for c in insp.get_columns("response")]
    with db.engine.begin() as conn:
        if "run_id" not in cols:
            conn.execute(sa_text("ALTER TABLE response ADD COLUMN run_id VARCHAR(36)"))
        if "is_complete" not in cols:
            conn.execute(sa_text("ALTER TABLE response ADD COLUMN is_complete BOOLEAN DEFAULT 0"))

# -------------------- 辅助函数 --------------------
def admin_required():
    """检查是否已登录管理员。"""
    return bool(session.get("admin"))

# -------------------- 首页 --------------------
@app.route("/")
def index():
    return render_template("index.html")

# -------------------- 实验开始 --------------------
@app.route("/start/<version>")
def start(version):
    # 仅允许 cn / jp
    version = (version or "").strip().lower()
    if version not in ("cn", "jp"):
        return "Invalid version (use 'cn' or 'jp')", 400

    # 读取并按版本筛选
    df = pd.read_csv("stimuli_list.csv")
    sub = df[df["version"].str.lower() == version].copy()
    if sub.empty:
        return f"No stimuli found for version={version}", 500

    # 转字典 + 打乱 + 固定 index
    records = sub.to_dict(orient="records")
    random.shuffle(records)
    for i, s in enumerate(records):
        s["stimulus_label"] = s.get("label") or s.get("stimulus_label") or f"item{i+1}"
        s["person"] = s.get("person", "")
        s["url"] = s.get("url", "")
        s["index"] = i  # 模板直接使用

    # 会话初始化
    session["participant_id"] = str(uuid.uuid4())[:8]
    session["version"] = version
    session["stimuli_order"] = records
    session["current_index"] = 0
    session["run_id"] = str(uuid.uuid4())

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
        # 越界保护
        if not (0 <= idx < len(stimuli_order)):
            return redirect(url_for("thank_you", version=version))

        # 取表单的 trial_index；异常时回退为 idx
        try:
            form_trial_index = int(request.form.get("trial_index", idx))
        except Exception:
            form_trial_index = idx

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
            run_id=session.get("run_id"),
            is_complete=False
        )
        db.session.add(r)
        db.session.commit()

        # 进入下一条或结束
        session["current_index"] = idx + 1
        if session["current_index"] >= len(stimuli_order):
            return redirect(url_for("thank_you", version=version))
        return redirect(url_for("experiment"))

    # GET：渲染当前条
    if idx >= len(stimuli_order):
        return redirect(url_for("thank_you", version=version))

    stim = stimuli_order[idx]
    start_time = datetime.datetime.utcnow().isoformat()

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
    v = (request.args.get("version") or session.get("version") or "cn").strip().lower()
    version = "cn" if v == "cn" else "jp"

    rid = session.get("run_id")
    if rid:
        try:
            db.session.execute(text("UPDATE response SET is_complete = 1 WHERE run_id = :rid"), {"rid": rid})
            db.session.commit()
        except Exception:
            db.session.rollback()

    return render_template("thank_you.html", version=version)

# -------------------- 管理员登录/退出 --------------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    error = None
    if request.method == "POST":
        if request.form.get("username") == ADMIN_USER and request.form.get("password") == ADMIN_PASS:
            session["admin"] = True
            return redirect(url_for("admin_panel"))
        error = "用户名或密码错误"
    return render_template("admin.html", error=error)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin"))

# -------------------- 管理员控制面板 --------------------
@app.route("/admin/panel")
def admin_panel():
    if not admin_required():
        return redirect(url_for("admin"))
    total_completed = db.session.query(Response).filter(Response.is_complete == True).count()
    total_partial   = db.session.query(Response).filter(
        (Response.is_complete == False) | (Response.is_complete == None)
    ).count()
    count = total_completed  # 你原来展示的是答卷总数，可改为已完成数量
    return render_template("admin_panel.html", count=count, partial=total_partial)

# -------------------- 导出 CSV --------------------
@app.route("/admin/export_csv")
def export_csv():
    if not admin_required():
        return redirect(url_for("admin"))

    stmt = select(Response).where(Response.is_complete == True)
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
    pd.DataFrame(records).to_csv(csv_buffer, index=False, encoding="utf-8-sig")

    mem = io.BytesIO(csv_buffer.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="responses.csv")

# -------------------- 导出数据库（下载 .db） --------------------
@app.route("/admin/download_db")
def download_db():
    if not admin_required():
        return redirect(url_for("admin"))
    return send_file(DB_PATH, as_attachment=True)

# 兼容老链接 /admin/export_db
@app.route("/admin/export_db")
def export_db_compat():
    return download_db()

# -------------------- 清空数据库（危险操作） --------------------
@app.post("/admin/clear_db")
def clear_db():
    if not admin_required():
        return redirect(url_for("admin"))
    # 简单二次确认：前端必须传 really=yes
    if request.form.get("really") != "yes":
        abort(400, description="Missing confirmation")
    # 删除所有记录
    deleted = db.session.query(Response).delete()
    db.session.commit()
    # 可选：整理 SQLite 文件
    try:
        db.session.execute(text("VACUUM"))
    except Exception:
        pass
    return redirect(url_for("admin_panel"))

# -------------------- 清空未完成记录 --------------------
@app.post("/admin/delete_partials")
def delete_partials():
    if not admin_required():
        return redirect(url_for("admin"))
    try:
        deleted = db.session.query(Response).filter(
            (Response.is_complete == False) | (Response.is_complete == None)
        ).delete(synchronize_session=False)
        db.session.commit()
        # 可选：整理数据库
        try:
            db.session.execute(text("VACUUM"))
        except Exception:
            pass
        from flask import flash
        flash(f"已删除未完成记录 {deleted} 条。")
    except Exception as e:
        db.session.rollback()
        from flask import flash
        flash(f"删除未完成记录失败：{e}")
    return redirect(url_for("admin_panel"))

# -------------------- 启动 --------------------
if __name__ == "__main__":
    app.run(debug=True)
