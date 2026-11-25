"""NEF Galerie - vytvoří z NEF souborů jeden HTML soubor pro výběr fotek klientem."""
import os, io, json, base64, struct, threading, queue, subprocess
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageDraw, ImageFont, ImageTk

EMAIL = "vas@email.cz"  # e-mail fotografa, kam klient pošle výběr
OUT_NAME = "vybrat_fotky_k_editaci"
ACC = "#e8b04a"


# ---------------------------------------------------------------- NEF čtení
def read_preview(path):
    """Vrátí (jpeg bytes, orientace, exif) – čte jen potřebné části souboru."""
    with open(path, "rb") as f:
        hdr = f.read(8)
        bo = "<" if hdr[:2] == b"II" else ">"

        def ifd(off):
            f.seek(off)
            n = struct.unpack(bo + "H", f.read(2))[0]
            raw = f.read(12 * n)
            tags = {}
            for k in range(n):
                tag, typ, cnt = struct.unpack(bo + "HHI", raw[12 * k:12 * k + 8])
                v = raw[12 * k + 8:12 * k + 12]
                if typ == 3 and cnt == 1:
                    tags[tag] = struct.unpack(bo + "H", v[:2])[0]
                elif typ in (4, 13):
                    if cnt == 1:
                        tags[tag] = struct.unpack(bo + "I", v)[0]
                    else:
                        tags[tag] = ("list", struct.unpack(bo + "I", v)[0], cnt)
            for tag, val in list(tags.items()):
                if isinstance(val, tuple):
                    f.seek(val[1])
                    tags[tag] = list(struct.unpack(bo + "%dI" % val[2], f.read(4 * val[2])))
            return tags

        t0 = ifd(struct.unpack(bo + "I", hdr[4:8])[0])
        subs = t0.get(330) or []
        if isinstance(subs, int):
            subs = [subs]
        best = None
        for s in subs:
            t = ifd(s)
            if 513 in t and 514 in t and (best is None or t[514] > best[1]):
                best = (t[513], t[514])
        if not best:
            raise ValueError("NEF neobsahuje náhled")
        f.seek(best[0])
        jpg = f.read(best[1])
    return jpg, t0.get(274, 1)


def orient(im, o):
    rot = {3: 180, 6: 270, 8: 90}.get(o)
    return im.rotate(rot, expand=True) if rot else im


def exif_info(path):
    try:
        with Image.open(path) as raw:
            ex = raw.getexif()
            dt = str(ex.get(306, ""))
            sub = ex.get_ifd(0x8769)
    except Exception:
        return "", ""

    def fl(v):
        try:
            return float(v)
        except Exception:
            return None
    info = []
    if sub.get(37386): info.append(f"{fl(sub[37386]):.0f} mm")
    if sub.get(33437): info.append(f"f/{fl(sub[33437]):g}")
    e = fl(sub.get(33434))
    if e: info.append(f"1/{round(1 / e)} s" if e < 1 else f"{e:g} s")
    if sub.get(34855): info.append(f"ISO {sub[34855]}")
    return dt, " · ".join(info)


def font(name, size):
    try:
        return ImageFont.truetype(name, size)
    except Exception:
        return ImageFont.load_default()


def stamp(im, name):
    im = im.convert("RGB")
    s = max(im.size)
    over = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    wf = font("arialbd.ttf", s // 17)
    tw = d.textbbox((0, 0), "NÁHLED", font=wf)
    t = Image.new("RGBA", (tw[2] + 40, tw[3] + 40), (0, 0, 0, 0))
    ImageDraw.Draw(t).text((20, 20), "NÁHLED", font=wf, fill=(255, 255, 255, 60))
    t = t.rotate(30, expand=True)
    over.paste(t, ((im.width - t.width) // 2, (im.height - t.height) // 2), t)
    nf = font("arial.ttf", s // 60)
    b = d.textbbox((0, 0), name, font=nf)
    pad = s // 170
    d.rectangle([0, im.height - b[3] - 2 * pad, b[2] + 2 * pad, im.height], fill=(0, 0, 0, 140))
    d.text((pad, im.height - b[3] - pad), name, font=nf, fill=(255, 255, 255, 230))
    return Image.alpha_composite(im.convert("RGBA"), over).convert("RGB")


def b64jpg(im, q):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def process(path, long_edge, quality):
    stem = os.path.splitext(os.path.basename(path))[0]
    jpg, o = read_preview(path)
    im = Image.open(io.BytesIO(jpg))
    im.draft("RGB", (long_edge, long_edge))
    im = orient(im.convert("RGB"), o)
    big = im.copy(); big.thumbnail((long_edge, long_edge), Image.LANCZOS)
    th = im.copy(); th.thumbnail((480, 480), Image.LANCZOS)
    dt, info = exif_info(path)
    return {"id": stem, "time": dt, "info": info,
            "t": b64jpg(th, 75), "i": b64jpg(stamp(big, stem), quality)}


def small_thumb(path):
    jpg, o = read_preview(path)
    im = Image.open(io.BytesIO(jpg))
    im.draft("RGB", (300, 300))
    im = orient(im.convert("RGB"), o)
    im.thumbnail((150, 150), Image.LANCZOS)
    return im


# ---------------------------------------------------------------- HTML
TEMPLATE = r"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE_HTML%%</title>
<style>
:root{--bg:#141414;--panel:#1e1e1e;--fg:#eee;--mute:#999;--acc:#e8b04a;--line:#2c2c2c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:rgba(20,20,20,.95);backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center}
h1{font-size:17px;font-weight:600;margin:0;flex:1 1 100%}
@media(min-width:800px){h1{flex:1 1 auto}}
.count{color:var(--acc);font-weight:600;font-variant-numeric:tabular-nums}
button,.btn{background:var(--panel);color:var(--fg);border:1px solid #3a3a3a;border-radius:8px;padding:8px 12px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
button:hover,.btn:hover{border-color:#666}
button.on,#send{background:var(--acc);color:#111;border-color:var(--acc)}
.intro{max-width:900px;margin:16px auto 0;padding:0 16px;color:var(--mute)}
.intro b{color:var(--fg)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;padding:16px}
@media(min-width:700px){.grid{grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}}
.card{position:relative;background:#000;border-radius:6px;overflow:hidden;aspect-ratio:1;cursor:zoom-in;outline:3px solid transparent}
.card img{width:100%;height:100%;object-fit:contain;display:block}
.card.sel{outline-color:var(--acc)}
.card .name{position:absolute;left:6px;bottom:6px;font-size:12px;background:rgba(0,0,0,.6);padding:2px 6px;border-radius:4px}
.card .note-dot{position:absolute;left:6px;top:6px;font-size:12px;background:rgba(0,0,0,.6);padding:2px 6px;border-radius:4px}
.heart{position:absolute;right:6px;top:6px;width:40px;height:40px;border-radius:50%;border:0;background:rgba(0,0,0,.55);display:grid;place-items:center;padding:0;font-size:21px;line-height:1;color:#fff}
.sel .heart{background:var(--acc);color:#111}
.hidden{display:none!important}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.97);z-index:10;display:flex;flex-direction:column}
#lb .stage{flex:1;display:flex;align-items:center;justify-content:center;min-height:0;position:relative}
#lb img{max-width:100%;max-height:100%;object-fit:contain;user-select:none}
#lb .nav{position:absolute;top:0;bottom:0;width:18%;background:none;border:0;font-size:44px;color:#fff;opacity:.5}
#lb .nav:hover{opacity:1}
#lb .prev{left:0;text-align:left;padding-left:12px}#lb .next{right:0;text-align:right;padding-right:12px}
#lb .bar{display:flex;flex-wrap:wrap;gap:8px 10px;align-items:center;padding:10px 16px;border-top:1px solid var(--line)}
#lb .bar .meta{flex:1 1 200px;color:var(--mute);font-size:13px}
#lb .bar .meta b{color:var(--fg);font-size:15px}
#lb input{flex:2 1 220px;background:var(--panel);color:var(--fg);border:1px solid #3a3a3a;border-radius:8px;padding:9px 10px;font:inherit}
#lbSel.on{background:var(--acc);color:#111}
dialog{background:var(--panel);color:var(--fg);border:1px solid #444;border-radius:12px;max-width:640px;width:calc(100% - 32px)}
dialog textarea{width:100%;height:220px;background:#111;color:var(--fg);border:1px solid #333;border-radius:8px;padding:10px;font:13px/1.5 ui-monospace,Consolas,monospace}
dialog::backdrop{background:rgba(0,0,0,.7)}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
kbd{background:#333;border-radius:4px;padding:0 5px;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>%%TITLE_HTML%%</h1>
  <span>Vybráno: <span class="count" id="cnt">0</span> / <span id="tot">0</span></span>
  <button id="fAll" class="on">Všechny</button>
  <button id="fSel">Jen vybrané</button>
  <button id="send">Odeslat výběr</button>
</header>
<p class="intro">Klikněte na fotku pro zvětšení, <b>srdíčkem ♥</b> ji vyberte k finální úpravě. K fotce můžete připsat poznámku (např. „ořez na výšku“, „černobíle“). Výběr se průběžně ukládá. Až budete hotovi, klikněte na <b>Odeslat výběr</b>. Ve zvětšení listujete šipkami <kbd>←</kbd> <kbd>→</kbd> nebo přejetím prstem, <kbd>mezerník</kbd> vybírá.<br>Jedná se o neupravené náhledy, barvy, expozice a retuš budou dořešeny ve finální úpravě.</p>
<main class="grid" id="grid"></main>

<div id="lb" class="hidden">
  <div class="stage">
    <button class="nav prev" aria-label="Předchozí">‹</button>
    <img id="lbImg" alt="">
    <button class="nav next" aria-label="Další">›</button>
  </div>
  <div class="bar">
    <div class="meta"><b id="lbName"></b> <span id="lbPos"></span><br><span id="lbInfo"></span></div>
    <input id="lbNote" placeholder="Poznámka k této fotce (nepovinné)">
    <button id="lbSel">♡ Vybrat</button>
    <button id="lbClose">Zavřít ✕</button>
  </div>
</div>

<dialog id="dlg">
  <h3 style="margin-top:0">Váš výběr</h3>
  <p style="color:var(--mute);margin-top:0">Pošlete výběr e-mailem na <b>%%EMAIL%%</b>, případně text zkopírujte a pošlete zprávou.</p>
  <textarea id="out" readonly></textarea>
  <div class="row">
    <a class="btn" id="mail" href="#" style="background:var(--acc);color:#111;border-color:var(--acc)">Poslat e-mailem</a>
    <button id="share" class="hidden">Sdílet…</button>
    <button id="copy">Zkopírovat</button>
    <button id="dl">Stáhnout .txt</button>
    <button id="dlgClose" style="margin-left:auto">Zavřít</button>
  </div>
</dialog>

<script>window.PHOTOS=%%DATA%%;window.GTITLE=%%TITLE_JS%%;</script>
<script>
(function(){
  var PH = window.PHOTOS || [];
  var KEY = 'proof:' + window.GTITLE + ':' + PH.length + ':' + (PH[0] ? PH[0].id : '');
  var EMAIL = '%%EMAIL%%';
  var state = {sel:{}, notes:{}};
  try { var s = JSON.parse(localStorage.getItem(KEY)); if (s && s.sel) state = s; } catch(e){}
  function save(){ try { localStorage.setItem(KEY, JSON.stringify(state)); } catch(e){} }
  function src(b){ return 'data:image/jpeg;base64,' + b; }
  var grid = document.getElementById('grid'), onlySel = false, cur = -1;
  document.getElementById('tot').textContent = PH.length;

  PH.forEach(function(p, i){
    var c = document.createElement('div'); c.className = 'card';
    c.innerHTML = '<img loading="lazy" alt=""><span class="name"></span><span class="note-dot hidden">✎</span><button class="heart" aria-label="Vybrat">♡</button>';
    c.querySelector('img').src = src(p.t); c.querySelector('.name').textContent = p.id;
    c.addEventListener('click', function(e){ if (e.target.closest('.heart')) { toggle(i); e.stopPropagation(); } else open(i); });
    grid.appendChild(c);
  });
  function render(){
    var n = 0;
    PH.forEach(function(p, i){
      var c = grid.children[i], s = !!state.sel[p.id]; if (s) n++;
      c.classList.toggle('sel', s);
      c.querySelector('.heart').textContent = s ? '♥' : '♡';
      c.querySelector('.note-dot').classList.toggle('hidden', !state.notes[p.id]);
      c.classList.toggle('hidden', onlySel && !s);
    });
    document.getElementById('cnt').textContent = n;
    if (cur >= 0) { var s2 = !!state.sel[PH[cur].id], b = document.getElementById('lbSel'); b.textContent = s2 ? '♥ Vybráno' : '♡ Vybrat'; b.classList.toggle('on', s2); }
  }
  function toggle(i){ var id = PH[i].id; if (state.sel[id]) delete state.sel[id]; else state.sel[id] = 1; save(); render(); }
  function visible(){ var v = []; PH.forEach(function(p, i){ if (!onlySel || state.sel[p.id]) v.push(i); }); return v; }
  function open(i){
    cur = i; var p = PH[i], v = visible();
    document.getElementById('lbImg').src = src(p.i);
    document.getElementById('lbName').textContent = p.id;
    document.getElementById('lbPos').textContent = '(' + (v.indexOf(i) + 1) + ' / ' + v.length + ')';
    document.getElementById('lbInfo').textContent = p.info || '';
    document.getElementById('lbNote').value = state.notes[p.id] || '';
    document.getElementById('lb').classList.remove('hidden'); document.body.style.overflow = 'hidden';
    render();
  }
  function close(){ document.getElementById('lb').classList.add('hidden'); document.getElementById('lbImg').removeAttribute('src'); document.body.style.overflow = ''; cur = -1; render(); }
  function step(d){ var v = visible(), k = v.indexOf(cur) + d; if (k >= 0 && k < v.length) open(v[k]); }
  document.querySelector('.prev').onclick = function(){ step(-1); };
  document.querySelector('.next').onclick = function(){ step(1); };
  document.getElementById('lbClose').onclick = close;
  document.getElementById('lbSel').onclick = function(){ toggle(cur); };
  document.getElementById('lbNote').addEventListener('input', function(e){ var id = PH[cur].id; if (e.target.value.trim()) state.notes[id] = e.target.value; else delete state.notes[id]; save(); });
  document.addEventListener('keydown', function(e){
    if (cur < 0) return;
    if (e.target.tagName === 'INPUT') { if (e.key === 'Escape') close(); return; }
    if (e.key === 'ArrowRight') step(1); else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'Escape') close(); else if (e.key === ' ') { e.preventDefault(); toggle(cur); }
  });
  var sx = null, stage = document.querySelector('#lb .stage');
  stage.addEventListener('touchstart', function(e){ sx = e.touches.length === 1 ? e.touches[0].clientX : null; }, {passive:true});
  stage.addEventListener('touchend', function(e){ if (sx === null) return; var dx = e.changedTouches[0].clientX - sx; if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1); sx = null; });

  document.getElementById('fAll').onclick = function(){ onlySel = false; this.classList.add('on'); document.getElementById('fSel').classList.remove('on'); render(); };
  document.getElementById('fSel').onclick = function(){ onlySel = true; this.classList.add('on'); document.getElementById('fAll').classList.remove('on'); render(); };

  function text(){
    var ids = PH.filter(function(p){ return state.sel[p.id]; }).map(function(p){ return p.id; });
    var t = window.GTITLE + '\nVýběr fotografií k úpravě – ' + ids.length + ' ks\n\n';
    ids.forEach(function(id){ t += id + (state.notes[id] ? '  – ' + state.notes[id] : '') + '\n'; });
    t += '\n--- Lightroom ---\n' + ids.join(' ') + '\n';
    return t;
  }
  var dlg = document.getElementById('dlg');
  if (navigator.share) document.getElementById('share').classList.remove('hidden');
  document.getElementById('send').onclick = function(){
    var t = text(); document.getElementById('out').value = t;
    document.getElementById('mail').href = 'mailto:' + EMAIL + '?subject=' + encodeURIComponent('Výběr fotografií – ' + window.GTITLE) + '&body=' + encodeURIComponent(t);
    document.getElementById('copy').textContent = 'Zkopírovat';
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
  };
  document.getElementById('dlgClose').onclick = function(){ if (dlg.close) dlg.close(); else dlg.removeAttribute('open'); };
  document.getElementById('share').onclick = function(){ navigator.share({title: 'Výběr fotografií', text: text()}).catch(function(){}); };
  document.getElementById('copy').onclick = function(){
    var ta = document.getElementById('out'), b = this, ok = false;
    ta.focus(); ta.select(); try { ok = document.execCommand('copy'); } catch(e){}
    if (!ok && navigator.clipboard) navigator.clipboard.writeText(ta.value).catch(function(){});
    b.textContent = 'Zkopírováno ✓';
  };
  document.getElementById('dl').onclick = function(){ var a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text()], {type:'text/plain;charset=utf-8'})); a.download = 'vyber-fotografii.txt'; document.body.appendChild(a); a.click(); a.remove(); };
  render();
})();
</script>
</body>
</html>
"""


def build_html(photos, title):
    import html as h
    data = json.dumps(photos, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    tj = json.dumps(title, ensure_ascii=False).replace("<", "\\u003c")
    return (TEMPLATE.replace("%%TITLE_HTML%%", h.escape(title))
            .replace("%%TITLE_JS%%", tj).replace("%%EMAIL%%", EMAIL)
            .replace("%%DATA%%", data))


# ---------------------------------------------------------------- GUI
class App(tk.Tk):
    TILE = 170

    def __init__(self):
        super().__init__()
        self.title("NEF Galerie – výběr fotek pro klienta")
        self.geometry("1100x760"); self.minsize(700, 500)
        self.configure(bg="#1b1b1b")
        self.q = queue.Queue()
        self.files, self.vars, self.tiles, self.imgs = [], [], [], {}
        self.folder = None; self.busy = False; self.load_gen = 0
        self._style(); self._build()
        self.after(60, self._poll)

    def _style(self):
        s = ttk.Style(self); s.theme_use("clam")
        bg, fg = "#1b1b1b", "#eeeeee"
        s.configure(".", background=bg, foreground=fg, fieldbackground="#262626", font=("Segoe UI", 10))
        s.configure("TButton", background="#2a2a2a", bordercolor="#444", padding=(10, 5))
        s.map("TButton", background=[("active", "#353535"), ("disabled", "#222")], foreground=[("disabled", "#666")])
        s.configure("Acc.TButton", background=ACC, foreground="#111", font=("Segoe UI", 11, "bold"), padding=(16, 8))
        s.map("Acc.TButton", background=[("active", "#f2c46c"), ("disabled", "#555")])
        s.configure("TCheckbutton", background="#222", foreground=fg)
        s.map("TCheckbutton", background=[("active", "#222")])
        s.configure("TRadiobutton", background=bg, foreground=fg)
        s.map("TRadiobutton", background=[("active", bg)])
        s.configure("Horizontal.TProgressbar", background=ACC, troughcolor="#262626", bordercolor="#333")
        s.configure("Mute.TLabel", foreground="#9a9a9a")

    def _build(self):
        top = ttk.Frame(self, padding=(14, 12, 14, 6)); top.pack(fill="x")
        ttk.Button(top, text="📁  Vybrat složku s NEF…", command=self.choose).pack(side="left")
        self.lblFolder = ttk.Label(top, text="Není vybrána žádná složka", style="Mute.TLabel")
        self.lblFolder.pack(side="left", padx=12)

        opt = ttk.Frame(self, padding=(14, 4)); opt.pack(fill="x")
        ttk.Label(opt, text="Název galerie:").pack(side="left")
        self.titleVar = tk.StringVar()
        ttk.Entry(opt, textvariable=self.titleVar, width=38).pack(side="left", padx=(6, 20))
        ttk.Label(opt, text="Velikost náhledů:").pack(side="left")
        self.sizeVar = tk.StringVar(value="1600")
        ttk.Radiobutton(opt, text="Standardní 1600 px (menší soubor)", variable=self.sizeVar, value="1600").pack(side="left", padx=6)
        ttk.Radiobutton(opt, text="Vysoká 2048 px", variable=self.sizeVar, value="2048").pack(side="left")

        bar = ttk.Frame(self, padding=(14, 6)); bar.pack(fill="x")
        ttk.Button(bar, text="Vybrat vše", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(bar, text="Zrušit vše", command=lambda: self.set_all(False)).pack(side="left", padx=6)
        ttk.Button(bar, text="Obrátit výběr", command=self.invert).pack(side="left")
        self.lblCount = ttk.Label(bar, text="", style="Mute.TLabel"); self.lblCount.pack(side="left", padx=14)
        ttk.Label(bar, text="Kliknutím na náhled soubor vyřadíte / zařadíte.", style="Mute.TLabel").pack(side="right")

        mid = tk.Frame(self, bg="#141414"); mid.pack(fill="both", expand=True, padx=14)
        self.canvas = tk.Canvas(mid, bg="#141414", highlightthickness=0)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg="#141414")
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.relayout())
        self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units"))
        self.placeholder = tk.Label(self.inner, text="\n\nVyberte složku s NEF soubory.", bg="#141414", fg="#777", font=("Segoe UI", 13))
        self.placeholder.grid(row=0, column=0, padx=20, pady=20)

        bot = ttk.Frame(self, padding=(14, 10)); bot.pack(fill="x")
        self.btnGen = ttk.Button(bot, text="Vygenerovat galerii", style="Acc.TButton", command=self.generate, state="disabled")
        self.btnGen.pack(side="right")
        self.prog = ttk.Progressbar(bot, mode="determinate"); self.prog.pack(side="left", fill="x", expand=True, padx=(0, 14))
        self.lblStatus = ttk.Label(bot, text="", style="Mute.TLabel"); self.lblStatus.pack(side="left", padx=(0, 14))

    # ---- folder / tiles
    def choose(self):
        if self.busy: return
        d = filedialog.askdirectory(title="Vyberte složku s NEF soubory")
        if not d: return
        files = sorted(f for f in os.listdir(d) if f.upper().endswith(".NEF"))
        if not files:
            messagebox.showwarning("NEF Galerie", "Ve vybrané složce nejsou žádné NEF soubory."); return
        self.folder = d; self.files = files
        self.lblFolder.configure(text=f"{d}   ({len(files)} NEF)")
        self.titleVar.set(os.path.basename(os.path.normpath(d)))
        for w in self.inner.winfo_children(): w.destroy()
        self.vars, self.tiles, self.imgs = [], [], {}
        for i, fn in enumerate(files):
            v = tk.BooleanVar(value=True)
            fr = tk.Frame(self.inner, bg="#222", highlightthickness=3, highlightbackground=ACC, width=self.TILE, height=self.TILE + 30)
            fr.pack_propagate(False)
            lbl = tk.Label(fr, bg="#000", text="…", fg="#555", cursor="hand2")
            lbl.pack(fill="both", expand=True)
            cb = ttk.Checkbutton(fr, text=os.path.splitext(fn)[0], variable=v, command=lambda i=i: self.refresh(i))
            cb.pack(fill="x", padx=4, pady=2)
            lbl.bind("<Button-1>", lambda e, i=i: (self.vars[i].set(not self.vars[i].get()), self.refresh(i)))
            self.vars.append(v); self.tiles.append((fr, lbl))
        self.canvas.yview_moveto(0); self.relayout(); self.refresh()
        self.load_gen += 1
        threading.Thread(target=self._load_thumbs, args=(d, files, self.load_gen), daemon=True).start()

    def _load_thumbs(self, d, files, gen):
        def one(i):
            try:
                im = small_thumb(os.path.join(d, files[i]))
            except Exception:
                im = None
            self.q.put(("thumb", gen, i, im))
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(one, range(len(files))))

    def relayout(self):
        if not self.tiles: return
        cols = max(1, (self.canvas.winfo_width() - 4) // (self.TILE + 10))
        for i, (fr, _) in enumerate(self.tiles):
            fr.grid(row=i // cols, column=i % cols, padx=5, pady=5)

    def refresh(self, i=None):
        idx = range(len(self.tiles)) if i is None else [i]
        for k in idx:
            on = self.vars[k].get(); fr, lbl = self.tiles[k]
            fr.configure(highlightbackground=ACC if on else "#333")
            if k in self.imgs:
                lbl.configure(image=self.imgs[k][0 if on else 1])
        n = sum(v.get() for v in self.vars)
        self.lblCount.configure(text=f"Do galerie půjde: {n} / {len(self.vars)}")
        self.btnGen.configure(state="normal" if n and not self.busy else "disabled")

    def set_all(self, val):
        for v in self.vars: v.set(val)
        self.refresh()

    def invert(self):
        for v in self.vars: v.set(not v.get())
        self.refresh()

    # ---- generate
    def generate(self):
        sel = [f for f, v in zip(self.files, self.vars) if v.get()]
        if not sel or self.busy: return
        title = self.titleVar.get().strip() or "Výběr fotografií"
        self.busy = True; self.btnGen.configure(state="disabled")
        self.prog.configure(maximum=len(sel), value=0)
        threading.Thread(target=self._gen, args=(self.folder, sel, title, int(self.sizeVar.get())), daemon=True).start()

    def _gen(self, d, sel, title, size):
        try:
            done = [0]; lock = threading.Lock()

            def one(fn):
                r = process(os.path.join(d, fn), size, 78)
                with lock:
                    done[0] += 1; self.q.put(("prog", done[0], len(sel)))
                return r
            with ThreadPoolExecutor(6) as ex:
                photos = list(ex.map(one, sel))
            photos.sort(key=lambda p: (p["time"], p["id"]))
            for p in photos: p.pop("time", None)
            self.q.put(("status", "Zapisuji soubor…"))
            out = os.path.join(d, OUT_NAME + ".html")
            with open(out, "w", encoding="utf-8") as f:
                f.write(build_html(photos, title))
            self.q.put(("done", out))
        except Exception as e:
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "thumb":
                    _, gen, i, im = m
                    if gen != self.load_gen or i >= len(self.tiles): continue
                    if im is None:
                        self.tiles[i][1].configure(text="chyba", fg="#c55"); continue
                    dim = Image.blend(im, Image.new("RGB", im.size, (0, 0, 0)), 0.72)
                    self.imgs[i] = (ImageTk.PhotoImage(im), ImageTk.PhotoImage(dim))
                    self.tiles[i][1].configure(text="")
                    self.refresh(i)
                elif m[0] == "prog":
                    self.prog.configure(value=m[1]); self.lblStatus.configure(text=f"Zpracováno {m[1]} / {m[2]}")
                elif m[0] == "status":
                    self.lblStatus.configure(text=m[1])
                elif m[0] == "done":
                    self.busy = False; self.refresh()
                    mb = os.path.getsize(m[1]) / 1048576
                    self.lblStatus.configure(text=f"Hotovo – {mb:.0f} MB")
                    if messagebox.askyesno("Hotovo", f"Galerie vytvořena:\n{m[1]}\n\nVelikost: {mb:.0f} MB\n\nOtevřít složku se souborem?"):
                        subprocess.Popen(["explorer", "/select,", os.path.normpath(m[1])])
                elif m[0] == "error":
                    self.busy = False; self.refresh(); self.lblStatus.configure(text="Chyba")
                    messagebox.showerror("Chyba", m[1])
        except queue.Empty:
            pass
        self.after(60, self._poll)


if __name__ == "__main__":
    App().mainloop()
