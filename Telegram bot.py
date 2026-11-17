import os, re, io, csv, glob, asyncio, tempfile, shutil, threading, logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from xml.sax.saxutils import escape

import requests
import yt_dlp
from google import genai
from google.genai import types
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(" ", "").split(",") if x}
MAX_BYTES = 49 * 1024 * 1024  # límite de Telegram para bots: 50 MB

client = genai.Client(api_key=GEMINI_API_KEY)
SYSTEM = ("Eres un asistente útil, amable y directo. Respondes en el idioma del usuario "
          "(por defecto español). Usa texto plano, sin formato Markdown (nada de ** ni #). "
          "Puedes usar emojis con moderación.")
chats = {}


# ---------- servidor mínimo para que Render/UptimeRobot mantengan vivo el bot ----------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    do_HEAD = do_GET
    def log_message(self, *a): pass

def start_health():
    port = int(os.environ.get("PORT", "10000"))
    threading.Thread(target=HTTPServer(("0.0.0.0", port), Ping).serve_forever, daemon=True).start()


# ---------- utilidades ----------
def allowed(uid): return not ALLOWED or uid in ALLOWED

def split_text(t, n=4000):
    return [t[i:i + n] for i in range(0, len(t), n)] or ["(sin respuesta)"]

def strip_fences(t):
    t = t.strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*?)\n```$", t, re.S)
    return m.group(1) if m else t

def clean(s):
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return s.replace("*", "").strip()

def get_chat(uid):
    if uid not in chats:
        chats[uid] = client.chats.create(
            model=MODEL, config=types.GenerateContentConfig(system_instruction=SYSTEM))
    return chats[uid]

def ask(uid, parts):
    return get_chat(uid).send_message(parts).text or "(sin respuesta)"


# ---------- descargas ----------
def spotify_query(url):
    title = None
    try:
        html = requests.get(url, headers={"Accept-Language": "en", "User-Agent": "Mozilla/5.0"},
                            timeout=15).text
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        if m:
            title = m.group(1).replace(" | Spotify", "").replace(" - song and lyrics by ", " ")
    except Exception:
        pass
    if not title:
        title = requests.get("https://open.spotify.com/oembed", params={"url": url},
                             timeout=15).json()["title"]
    return f"ytsearch1:{title} audio"

def download(target, mode, outdir):
    opts = {"outtmpl": f"{outdir}/%(title).80s.%(ext)s", "noplaylist": True, "quiet": True,
            "no_warnings": True, "restrictfilenames": True, "max_filesize": MAX_BYTES}
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = "cookies.txt"
    if mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": "128"}]
    else:
        opts["format"] = "b[height<=720][ext=mp4]/b[height<=720]/b"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([target])
    files = [f for f in glob.glob(f"{outdir}/*") if not f.endswith(".part")]
    if not files:
        raise RuntimeError("No se generó ningún archivo (puede pesar más de 50 MB).")
    return max(files, key=os.path.getsize)

def direct_download(url, outdir):
    try:
        r = requests.get(url, stream=True, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        ctype = r.headers.get("content-type", "")
        if not ctype.startswith(("image/", "video/", "audio/", "application/pdf")):
            return None
        ext = ctype.split("/")[1].split(";")[0].replace("jpeg", "jpg")
        path = os.path.join(outdir, f"archivo.{ext}")
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    return None
                f.write(chunk)
        return path
    except Exception:
        return None

async def send_file(msg, path):
    ext = path.rsplit(".", 1)[-1].lower()
    kw = dict(read_timeout=180, write_timeout=180, connect_timeout=30)
    with open(path, "rb") as f:
        if ext in ("mp3", "m4a", "opus", "ogg"):
            await msg.reply_audio(f, **kw)
        elif ext in ("mp4", "webm", "mkv", "mov"):
            await msg.reply_video(f, supports_streaming=True, **kw)
        elif ext in ("jpg", "jpeg", "png", "webp") and os.path.getsize(path) < 10_000_000:
            await msg.reply_photo(f, **kw)
        else:
            await msg.reply_document(f, **kw)

async def run_download(msg, url, mode):
    tmp = tempfile.mkdtemp()
    try:
        await msg.chat.send_action("upload_document")
        target = url
        if "open.spotify.com" in url:
            if "/track/" not in url:
                await msg.reply_text("Por ahora solo puedo con canciones individuales de Spotify "
                                     "(enlaces /track/), no álbumes ni playlists.")
                return
            target = await asyncio.to_thread(spotify_query, url)
        path, err = None, None
        try:
            path = await asyncio.to_thread(download, target, mode, tmp)
        except Exception as e:
            err = e
        if not path:
            path = await asyncio.to_thread(direct_download, url, tmp)
        if not path:
            await msg.reply_text(f"😕 No pude descargarlo.\n{str(err)[:300] if err else ''}")
            return
        if os.path.getsize(path) > MAX_BYTES:
            await msg.reply_text("El archivo pesa más de 50 MB y Telegram no me deja enviarlo.")
            return
        await send_file(msg, path)
    except Exception as e:
        log.exception("download error")
        await msg.reply_text(f"😕 Error al descargar: {str(e)[:300]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- generación de archivos ----------
FILE_RE = re.compile(
    r"(haz|hazme|crea|crear|creame|créame|genera|generar|dame|pasame|pásame|exporta|"
    r"convierte|prepara|escribe).{0,200}?\b(pdf|word|docx|excel|xlsx|csv|txt|json|html|markdown|md)\b",
    re.I | re.S)
KIND = {"word": "docx", "excel": "xlsx", "markdown": "md"}

def gen_content(kind, request):
    if kind in ("xlsx", "csv"):
        inst = "Responde SOLO con CSV válido (primera fila encabezados), sin explicaciones ni bloques de código."
    elif kind == "json":
        inst = "Responde SOLO con JSON válido, sin explicaciones ni bloques de código."
    elif kind == "html":
        inst = "Responde SOLO con un documento HTML completo, sin bloques de código."
    else:
        inst = ("Responde SOLO con el contenido del documento en Markdown simple: títulos con #, ##, "
                "listas con -, y párrafos. Sin explicaciones fuera del documento.")
    r = client.models.generate_content(
        model=MODEL, contents=request,
        config=types.GenerateContentConfig(system_instruction=inst))
    return strip_fences(r.text or "")

def build_docx(md, path):
    from docx import Document
    d = Document()
    for line in md.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            d.add_heading(clean(m.group(2)), level=len(m.group(1)))
        elif re.match(r"^\s*[-*•]\s+", line):
            d.add_paragraph(clean(re.sub(r"^\s*[-*•]\s+", "", line)), style="List Bullet")
        elif re.match(r"^\s*\d+[.)]\s+", line):
            d.add_paragraph(clean(re.sub(r"^\s*\d+[.)]\s+", "", line)), style="List Number")
        else:
            d.add_paragraph(clean(line))
    d.save(path)

def build_pdf(md, path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    st = getSampleStyleSheet()
    fix = lambda s: escape(clean(s).encode("cp1252", "ignore").decode("cp1252"))
    story = []
    for line in md.splitlines():
        if not line.strip():
            story.append(Spacer(1, 6)); continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            story.append(Paragraph(fix(m.group(2)), st[f"Heading{len(m.group(1))}"]))
        elif re.match(r"^\s*[-*•]\s+", line):
            story.append(Paragraph("• " + fix(re.sub(r"^\s*[-*•]\s+", "", line)), st["BodyText"]))
        else:
            story.append(Paragraph(fix(line), st["BodyText"]))
    SimpleDocTemplate(path, pagesize=A4).build(story)

def build_xlsx(text, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook(); ws = wb.active
    for row in csv.reader(io.StringIO(text)):
        ws.append(row)
    for c in ws[1]:
        c.font = Font(bold=True)
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(
            50, max(len(str(c.value or "")) for c in col) + 2)
    wb.save(path)

async def handle_file_request(update, kind, request):
    msg = update.message
    await msg.chat.send_action("upload_document")
    content = await asyncio.to_thread(gen_content, kind, request)
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, f"documento.{kind}")
        if kind == "docx": build_docx(content, path)
        elif kind == "pdf": build_pdf(content, path)
        elif kind == "xlsx": build_xlsx(content, path)
        else:
            with open(path, "w", encoding="utf-8") as f: f.write(content)
        with open(path, "rb") as f:
            await msg.reply_document(f, read_timeout=120, write_timeout=120)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id): return
    await update.message.reply_text(
        "👋 ¡Hola! Soy tu asistente con IA.\n\n"
        "🔗 Envíame un enlace (YouTube, TikTok, Instagram, X, Spotify, etc.) y lo descargo.\n"
        "💬 Pregúntame lo que quieras.\n"
        "📄 Pídeme archivos: «hazme un PDF con…», «crea un Excel de…», «dame un Word sobre…».\n"
        "🖼 Envíame una foto y la analizo.\n\n"
        "/nuevo reinicia la conversación.")

async def nuevo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chats.pop(update.effective_user.id, None)
    await update.message.reply_text("🧹 Conversación reiniciada.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    text = update.message.text or ""
    try:
        m = re.search(r"https?://\S+", text)
        if m:
            url = m.group(0)
            if "open.spotify.com" in url:
                await run_download(update.message, url, "audio")
                return
            key = str(update.message.message_id)
            context.user_data.setdefault("urls", {})[key] = url
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Video", callback_data=f"video|{key}"),
                InlineKeyboardButton("🎵 Audio MP3", callback_data=f"audio|{key}")]])
            await update.message.reply_text("¿Qué quieres descargar?", reply_markup=kb)
            return
        fm = FILE_RE.search(text)
        if fm:
            kind = fm.group(2).lower()
            await handle_file_request(update, KIND.get(kind, kind), text)
            return
        await update.message.chat.send_action("typing")
        answer = await asyncio.to_thread(ask, uid, text)
        for part in split_text(answer):
            await update.message.reply_text(part)
    except Exception as e:
        log.exception("text error")
        await update.message.reply_text(f"⚠️ Ocurrió un error: {str(e)[:300]}")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid): return
    try:
        await update.message.chat.send_action("typing")
        f = await update.message.photo[-1].get_file()
        data = bytes(await f.download_as_bytearray())
        parts = [types.Part.from_bytes(data=data, mime_type="image/jpeg"),
                 update.message.caption or "Describe esta imagen y ayúdame con lo que se vea."]
        answer = await asyncio.to_thread(ask, uid, parts)
        for part in split_text(answer):
            await update.message.reply_text(part)
    except Exception as e:
        log.exception("photo error")
        await update.message.reply_text(f"⚠️ Ocurrió un error: {str(e)[:300]}")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(q.from_user.id):
        await q.answer(); return
    await q.answer()
    mode, key = q.data.split("|", 1)
    url = context.user_data.get("urls", {}).get(key)
    if not url:
        await q.edit_message_text("Ese enlace expiró, envíalo otra vez."); return
    await q.edit_message_text("⏳ Descargando...")
    await run_download(q.message, url, mode)


def main():
    start_health()
    app = (Application.builder().token(TELEGRAM_TOKEN)
           .read_timeout(120).write_timeout(120).connect_timeout(30).pool_timeout(30).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nuevo", nuevo))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()
