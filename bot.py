"""Sleeo Watch Server : veille Vinted 24h/24, indépendante de ton PC.
- Lit les pages de recherche Vinted toutes les ~20 s (niches intégrées + filtres des membres), via des proxys si fournis.
- Poste dans Discord : 3 photos, prix + frais, vendeur et avis, boutons ; enregistre aussi dans Supabase pour le site.
Variables : DISCORD_BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY (SECRÈTE), SITE_URL, GUILD_ID (facultatif, rôles),
            PROXY_URLS (facultatif : proxy1,proxy2 au format http://user:pass@host:port), CONCURRENCY (défaut 4)
"""
import os, re, json, time, asyncio, random
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import aiohttp, discord
from bs4 import BeautifulSoup
from discord.ext import commands, tasks
from dotenv import load_dotenv

try:
    from keep_alive import keep_alive
    keep_alive()
except ImportError:
    pass
load_dotenv()

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
SB_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SITE_URL = os.environ.get("SITE_URL", "https://sleeo.lovable.app")
GUILD_ID = os.environ.get("GUILD_ID")
PROXIES = [p.strip() for p in (os.environ.get("PROXY_URLS") or "").split(",") if p.strip()]
CONCURRENCY = int(os.environ.get("CONCURRENCY", "4"))
INTERVAL_FAST, INTERVAL_SLOW = 20, 60           # niches et bot avancé : 20 s ; bot basique : 60 s
QUOTA = {"bot_adv": 20, "bot_basic": 3}
MAX_NEW_PER_SCAN = 8
COLOR = 0xFF6B4A
URL_RE = re.compile(r"^https://www\.vinted\.[a-z.]+/catalog")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
if not (TOKEN and SB_URL and SB_KEY):
    raise RuntimeError("Variables manquantes : DISCORD_BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY")

intents = discord.Intents.default(); intents.message_content = True; intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
STATS = {"ok": 0, "fail": 0, "blocked": 0, "posted": 0, "last_status": 0, "since": time.time()}

# ───────────── Supabase ─────────────
_sb_session = None
async def sb(method, path, prefer="return=representation", **kw):
    global _sb_session
    if _sb_session is None or _sb_session.closed:
        _sb_session = aiohttp.ClientSession()
    h = {"apikey": SB_KEY, "Content-Type": "application/json", "Prefer": prefer}
    if not SB_KEY.startswith("sb_"):
        h["Authorization"] = f"Bearer {SB_KEY}"
    try:
        async with _sb_session.request(method, f"{SB_URL}/rest/v1/{path}", headers=h, timeout=aiohttp.ClientTimeout(total=20), **kw) as r:
            if r.status >= 300:
                print(f"[supabase] {method} {path} -> {r.status} {(await r.text())[:200]}")
                return None
            return [] if (r.status == 204 or prefer.endswith("minimal")) else await r.json(content_type=None)
    except Exception as e:
        print(f"[supabase] {e}")
        return None

# ───────────── Récupération de Vinted (proxys, blocages) ─────────────
class Fetcher:
    def __init__(self):
        self.sessions, self.bad, self.i = {}, {}, 0
    def pick(self):
        pool = PROXIES or [None]
        ok = [p for p in pool if self.bad.get(p, 0) < time.time()] or pool
        self.i += 1
        return ok[self.i % len(ok)]
    def session(self, proxy):
        s = self.sessions.get(proxy)
        if s is None or s.closed:
            s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
            self.sessions[proxy] = s
        return s
    async def get(self, url, tries=3):
        for _ in range(tries):
            proxy = self.pick()
            try:
                async with self.session(proxy).get(url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=20), headers={
                        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "fr-FR,fr;q=0.9"}) as r:
                    STATS["last_status"] = r.status
                    if r.status == 200:
                        STATS["ok"] += 1
                        return await r.text()
                    if r.status in (403, 429, 503):
                        STATS["blocked"] += 1
                        self.bad[proxy] = time.time() + 120
                    await asyncio.sleep(1 + random.random())
            except Exception as e:
                STATS["fail"] += 1
                self.bad[proxy] = time.time() + 30
        return None
fetcher = Fetcher()

# ───────────── Lecture des pages ─────────────
def num(t):
    m = re.search(r"\d+(?:[.,]\d+)?", (t or "").replace("\u00a0", "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None

def parse_cards(html):
    soup, out = BeautifulSoup(html, "html.parser"), []
    for card in soup.select('[data-testid="grid-item"]'):
        box = card.select_one('[data-testid^="product-item-id-"]')
        m = box and re.match(r"^product-item-id-(\d+)$", box.get("data-testid", ""))
        if not m:
            continue
        i = m.group(1)
        link = card.select_one(f'[data-testid="product-item-id-{i}--overlay-link"]')
        title = (link.get("title") if link else "") or ""
        pick = lambda rx: (re.search(rx, title) or [None, ""])[1].strip()
        img = card.select_one(f'[data-testid="product-item-id-{i}--image--img"]')
        price = card.select_one(f'[data-testid="product-item-id-{i}--price-text"]')
        total = card.select_one('[data-testid="total-combined-price"]')
        fav = card.select_one('[data-testid="favourite-count-text"]')
        badge = card.select_one(f'[data-testid="product-item-id-{i}--secondary-badge--content"]')
        href = (link.get("href") if link else "") or ""
        out.append({"id": i, "title": title.split(", Marque:")[0] or title, "brand": pick(r"Marque:\s*([^,]+)"), "state": pick(r"État:\s*([^,]+)"),
                    "size": pick(r"Taille:\s*([^,]+)"), "price": num(price.get_text() if price else ""), "total": num(total.get_text() if total else ""),
                    "image": img.get("src") if img else "", "url": "https://www.vinted.fr" + href.split("?")[0] if href.startswith("/") else href.split("?")[0],
                    "fav": int(num(fav.get_text()) or 0) if fav else 0, "demand": bool(badge and "demande" in badge.get_text().lower())})
    return out

async def fetch_detail(url):
    """3 photos, vendeur et avis, depuis la page de l'annonce (JSON-LD + données de la page). Tout est facultatif."""
    d = {"images": [], "seller": None, "seller_id": None, "rating": None, "reviews": None, "created": None}
    html = await fetcher.get(url, tries=2)
    if not html:
        return d
    try:
        for s in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
            try:
                j = json.loads(s.string or "")
            except Exception:
                continue
            for o in (j if isinstance(j, list) else [j]):
                if isinstance(o, dict) and (o.get("@type") == "Product" or "offers" in o):
                    im = o.get("image") or []
                    d["images"] = [x if isinstance(x, str) else x.get("url", "") for x in (im if isinstance(im, list) else [im])][:3]
        g = lambda rx: (re.search(rx, html) or [None, None])[1]
        d["rating"] = float(g(r'"feedback_reputation":\s*([0-9.]+)') or 0) or None
        d["reviews"] = int(g(r'"feedback_count":\s*(\d+)') or 0) or None
        d["seller_id"] = g(r'"profile_url":"https://www\.vinted\.[a-z.]+/member/(\d+)')
        d["seller"] = g(r'"login":"([^"]+)"')
        ts = g(r'"created_at_ts":\s*"?(\d{9,})')
        d["created"] = int(ts) if ts else None
    except Exception as e:
        print(f"[detail] {e}")
    return d

# ───────────── Publication Discord ─────────────
def stars(r, n):
    if not n or not r:
        return "Aucun avis"
    r = r / 100 if r > 5 else r
    k = max(1, min(5, round(r * 5 if r <= 1 else r)))
    return "⭐" * k + "☆" * (5 - k) + f" ({n})"

class ItemView(discord.ui.View):
    def __init__(self, c, det):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Acheter", style=discord.ButtonStyle.link, url=c["url"], emoji="🛒"))
        self.add_item(discord.ui.Button(label="Offre", style=discord.ButtonStyle.link, url=c["url"], emoji="💬"))
        self.add_item(discord.ui.Button(label="Favori", style=discord.ButtonStyle.link, url=c["url"], emoji="❤️"))
        if det.get("seller_id"):
            self.add_item(discord.ui.Button(label="Vendeur", style=discord.ButtonStyle.link, url=f"https://www.vinted.fr/member/{det['seller_id']}", emoji="👤"))

async def post_item(channel_id, c, det, label):
    ch = bot.get_channel(int(channel_id)) if channel_id else None
    if not ch:
        return
    imgs = [x for x in ([c["image"]] if c["image"] else []) + det["images"] if x]
    imgs = list(dict.fromkeys(imgs))[:3] or []
    e = discord.Embed(title=("🔥 " if c["demand"] else "") + c["title"][:240], url=c["url"], color=COLOR)
    e.add_field(name="💰 Prix", value=(f"**{c['price']:.2f} €**" if c["price"] is not None else "—") + (f"\n{c['total']:.2f} € frais inclus" if c["total"] else ""))
    e.add_field(name="🏷️ Marque", value=c["brand"] or "—"); e.add_field(name="👕 Taille", value=c["size"] or "—")
    e.add_field(name="🧼 État", value=c["state"] or "—"); e.add_field(name="❤️ Favoris", value=str(c["fav"]))
    e.add_field(name="👤 Vendeur", value=(det["seller"] or "—") + "\n" + stars(det["rating"], det["reviews"]))
    if det.get("created"):
        e.add_field(name="🕒 Mis en ligne", value=f"<t:{det['created']}:R>")
    if imgs:
        e.set_image(url=imgs[0])
    e.set_footer(text=f"Sleeo Watch · {label}")
    embeds = [e] + [discord.Embed(url=c["url"], color=COLOR).set_image(url=u) for u in imgs[1:3]]   # même url = galerie de 3 photos
    try:
        await ch.send(embeds=embeds, view=ItemView(c, det))
        STATS["posted"] += 1
    except Exception as ex:
        print(f"[discord] {ex}")

# ───────────── Sources à surveiller ─────────────
def with_order(url):
    p = urlparse(url); q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in ("order", "page", "time")]
    return urlunparse(p._replace(query=urlencode(q + [("order", "newest_first")])))

async def load_sources():
    src = {}
    for n in await sb("GET", "niches?active=eq.true&select=id,nom,search_url,discord_channel_id") or []:
        s = src.setdefault(with_order(n["search_url"]), {"interval": INTERVAL_FAST, "targets": []})
        s["targets"].append({"kind": "niche", "id": n["id"], "nom": n["nom"], "channel": n["discord_channel_id"]})
    ents = await sb("GET", "entitlements?active=eq.true&module_key=in.(bot_basic,bot_adv)&select=user_id,module_key,ends_at") or []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    adv, basic = set(), set()
    for e in ents:
        if e.get("ends_at") and e["ends_at"] < now:
            continue
        (adv if e["module_key"] == "bot_adv" else basic).add(e["user_id"])
    per_user = {}
    for a in await sb("GET", "alerts?active=eq.true&select=id,user_id,nom,search_url,channel,discord_channel_id&order=created_at.asc&limit=2000") or []:
        u = a["user_id"]
        if u not in adv and u not in basic or not URL_RE.match(a["search_url"] or ""):
            continue
        per_user[u] = per_user.get(u, 0) + 1
        if per_user[u] > (QUOTA["bot_adv"] if u in adv else QUOTA["bot_basic"]):
            continue
        s = src.setdefault(with_order(a["search_url"]), {"interval": INTERVAL_FAST if u in adv else INTERVAL_SLOW, "targets": []})
        s["interval"] = min(s["interval"], INTERVAL_FAST if u in adv else INTERVAL_SLOW)
        s["targets"].append({"kind": "alert", "id": a["id"], "user": u, "nom": a["nom"], "channel": a["discord_channel_id"] if a["channel"] == "discord" else None})
    return src

SEEN, NEXT, RUNNING = {}, {}, set()

async def scan(url, s):
    html = await fetcher.get(url)
    cards = parse_cards(html) if html else []
    if not cards:
        return
    seen = SEEN.get(url)
    if seen is None:                                    # première lecture : on mémorise sans publier
        SEEN[url] = {c["id"] for c in cards}
        return
    new = [c for c in cards if c["id"] not in seen]
    seen.update(c["id"] for c in cards)
    if len(seen) > 1500:
        SEEN[url] = {c["id"] for c in cards}
    for c in reversed(new[:MAX_NEW_PER_SCAN]):
        det = await fetch_detail(c["url"])
        imgs = list(dict.fromkeys(([c["image"]] if c["image"] else []) + det["images"]))[:3]
        for t in s["targets"]:
            row = {"item_id": int(c["id"]), "title": c["title"][:200], "price": c["price"], "total_price": c["total"], "brand": c["brand"] or None, "size": c["size"] or None,
                   "etat": c["state"] or None, "favoris": c["fav"], "url": c["url"], "images": imgs, "seller_login": det["seller"], "seller_rating": det["rating"], "seller_reviews": det["reviews"]}
            if t["kind"] == "niche":
                await sb("POST", "niche_hits?on_conflict=niche_id,item_id", prefer="resolution=ignore-duplicates,return=minimal", json={**row, "niche_id": t["id"]})
            else:
                await sb("POST", "alert_hits?on_conflict=alert_id,item_id", prefer="resolution=ignore-duplicates,return=minimal",
                         json={**row, "alert_id": t["id"], "user_id": t["user"], "currency": "EUR", "image": imgs[0] if imgs else None, "posted": True})
            await post_item(t["channel"], c, det, t["nom"] or "")
            await asyncio.sleep(1)

async def run_source(url, s, sem):
    async with sem:
        try:
            await scan(url, s)
        except Exception as e:
            print(f"[scan] {e}")
        finally:
            RUNNING.discard(url)

@tasks.loop(seconds=2)
async def scheduler():
    sem = scheduler.sem
    now = time.time()
    for url, s in scheduler.sources.items():
        if url in RUNNING or NEXT.get(url, 0) > now:
            continue
        NEXT[url] = now + s["interval"] + random.uniform(0, 3)
        RUNNING.add(url)
        asyncio.create_task(run_source(url, s, sem))

@tasks.loop(minutes=2)
async def refresh_sources():
    scheduler.sources = await load_sources()

@tasks.loop(seconds=30)
async def heartbeat():
    await sb("POST", "bot_status?on_conflict=id", prefer="resolution=merge-duplicates,return=minimal", json={"id": 1, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data": {**STATS, "sources": len(scheduler.sources), "proxies": len(PROXIES), "uptime_min": int((time.time() - STATS["since"]) / 60)}})

@tasks.loop(hours=24)
async def purge():
    await sb("POST", "rpc/purge_hits", json={})

@tasks.loop(minutes=5)
async def sync_roles():
    """Rôles Start / Plus / Max selon l'abonnement (nécessite GUILD_ID et les rôles du même nom)."""
    g = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if not g:
        return
    roles = {n.lower(): discord.utils.get(g.roles, name=n) for n in ("Start", "Plus", "Max")}
    tier = {s["user_id"]: (s["tier"] or "").lower() for s in await sb("GET", "subscriptions?status=eq.active&select=user_id,tier") or []}
    for p in await sb("GET", "profiles?discord_id=not.is.null&select=id,discord_id") or []:
        m = g.get_member(int(p["discord_id"]))
        if not m:
            continue
        want = roles.get(tier.get(p["id"], ""))
        try:
            rm = [r for r in roles.values() if r and r != want and r in m.roles]
            if rm: await m.remove_roles(*rm)
            if want and want not in m.roles: await m.add_roles(want)
        except Exception as e:
            print(f"[roles] {e}")

# ───────────── Panneau Discord (filtres personnels) ─────────────
async def account_of(discord_id):
    rows = await sb("GET", f"profiles?discord_id=eq.{int(discord_id)}&select=id")
    if not rows:
        return None
    mods = await sb("POST", "rpc/active_modules", json={"uid": rows[0]["id"]}) or []
    return {"uid": rows[0]["id"], "quota": max([QUOTA[m] for m in mods if m in QUOTA], default=0)}

NO_ACCOUNT = f"🔗 Relie d'abord ton compte Discord à ton compte Sleeo : **{SITE_URL}** > Mon compte > Connecter Discord."

class FilterModal(discord.ui.Modal, title="🛍️ Ajouter un filtre Vinted"):
    name = discord.ui.TextInput(label="Nom du filtre", required=False, max_length=60)
    url = discord.ui.TextInput(label="Lien de recherche Vinted", placeholder="https://www.vinted.fr/catalog?search_text=...", style=discord.TextStyle.long, max_length=500)
    async def on_submit(self, i):
        await i.response.defer(ephemeral=True)
        link = self.url.value.strip()
        if not URL_RE.match(link):
            return await i.followup.send("❌ Colle un lien `https://www.vinted.xx/catalog?...`.", ephemeral=True)
        acc = await account_of(i.user.id)
        if not acc: return await i.followup.send(NO_ACCOUNT, ephemeral=True)
        if acc["quota"] == 0: return await i.followup.send(f"🔒 Il te faut le module Bot : {SITE_URL}", ephemeral=True)
        mine = await sb("GET", f"alerts?user_id=eq.{acc['uid']}&active=eq.true&select=id") or []
        if len(mine) >= acc["quota"]: return await i.followup.send(f"⚠️ Limite atteinte : {acc['quota']} filtres pour ton palier.", ephemeral=True)
        ok = await sb("POST", "alerts", json={"user_id": acc["uid"], "nom": self.name.value.strip() or link[:60], "search_url": link, "channel": "discord", "discord_channel_id": str(i.channel_id), "active": True})
        await i.followup.send(f"✅ Filtre ajouté ({len(mine) + 1}/{acc['quota']}). Les annonces arriveront dans ce salon en moins d'une minute." if ok else "❌ Enregistrement impossible.", ephemeral=True)

class DeleteSelect(discord.ui.Select):
    def __init__(self, rows):
        super().__init__(placeholder="Filtre à supprimer", options=[discord.SelectOption(label=(r["nom"] or r["search_url"])[:90], value=r["id"]) for r in rows[:25]])
    async def callback(self, i):
        await sb("PATCH", f"alerts?id=eq.{self.values[0]}", json={"active": False}, prefer="return=minimal")
        await i.response.edit_message(content="✅ Filtre supprimé.", view=None)

class PanelView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Ajouter un filtre", style=discord.ButtonStyle.success, custom_id="sl_add", emoji="➕")
    async def add(self, i, b): await i.response.send_modal(FilterModal())
    @discord.ui.button(label="Mes filtres", style=discord.ButtonStyle.secondary, custom_id="sl_list", emoji="📋")
    async def lst(self, i, b):
        acc = await account_of(i.user.id)
        if not acc: return await i.response.send_message(NO_ACCOUNT, ephemeral=True)
        rows = await sb("GET", f"alerts?user_id=eq.{acc['uid']}&active=eq.true&select=nom,discord_channel_id&order=created_at.asc") or []
        txt = "\n".join(f"• **{r['nom']}**" + (f" → <#{r['discord_channel_id']}>" if r.get("discord_channel_id") else " (site)") for r in rows)
        await i.response.send_message((txt or "Aucun filtre actif.") + f"\n\nQuota : {len(rows)}/{acc['quota']}", ephemeral=True)
    @discord.ui.button(label="Supprimer un filtre", style=discord.ButtonStyle.danger, custom_id="sl_del", emoji="🗑️")
    async def delete(self, i, b):
        acc = await account_of(i.user.id)
        if not acc: return await i.response.send_message(NO_ACCOUNT, ephemeral=True)
        rows = await sb("GET", f"alerts?user_id=eq.{acc['uid']}&active=eq.true&select=id,nom,search_url&order=created_at.asc") or []
        if not rows: return await i.response.send_message("Aucun filtre actif.", ephemeral=True)
        v = discord.ui.View(timeout=60); v.add_item(DeleteSelect(rows))
        await i.response.send_message("Quel filtre supprimer ?", view=v, ephemeral=True)

@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup(ctx):
    await ctx.channel.send(embed=discord.Embed(title="🛍️ Sleeo Watch, veille Vinted 24h/24", color=COLOR, description=(
        "Reçois ici les nouvelles annonces de tes recherches, avec 3 photos, le prix, le vendeur et ses avis.\n\n"
        "1️⃣ Sur vinted.fr, applique tes filtres\n2️⃣ Copie le lien de la barre d'adresse\n3️⃣ Clique **➕ Ajouter un filtre**\n\n"
        f"Compte Sleeo requis : {SITE_URL}")), view=PanelView())

@bot.command(name="status")
@commands.has_permissions(administrator=True)
async def status(ctx):
    up = int((time.time() - STATS["since"]) / 60)
    await ctx.send(f"🟢 En ligne depuis {up} min · sources : {len(scheduler.sources)} · pages OK : {STATS['ok']} · bloquées : {STATS['blocked']} · erreurs : {STATS['fail']} · annonces postées : {STATS['posted']} · dernier statut HTTP : {STATS['last_status']} · proxys : {len(PROXIES)}")

@bot.event
async def on_ready():
    print(f"✅ Connecté : {bot.user}")
    bot.add_view(PanelView())
    if not hasattr(scheduler, "sources"):
        scheduler.sources, scheduler.sem = {}, asyncio.Semaphore(CONCURRENCY)
    for t in (refresh_sources, scheduler, heartbeat, purge, sync_roles):
        if not t.is_running():
            t.start()

bot.run(TOKEN)
