"""Sleeo Watch : bot de veille Vinted, relié aux comptes Sleeo (Supabase).
Changements par rapport à AURUM Watch :
 - les filtres sont enregistrés dans Supabase (table alerts) et survivent aux redémarrages
 - il faut un compte Sleeo relié à Discord et le module bot_basic ou bot_adv
 - quota de filtres selon le palier
 - chacun ne peut arrêter que ses propres filtres
 - le cookie Vinted est mis en cache (plus une requête de cookie toutes les 15 s)
"""
import os, re, time, asyncio
from urllib.parse import urlparse, parse_qs, urlencode
import aiohttp
import discord
from yarl import URL
from discord.ext import commands, tasks
from dotenv import load_dotenv

try:
    from keep_alive import keep_alive  # petit serveur web pour Render
    keep_alive()
except ImportError:
    pass

load_dotenv()
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")  # SECRET : seulement sur le serveur du bot
if not (BOT_TOKEN and SUPABASE_URL and SUPABASE_KEY):
    raise RuntimeError("Variables manquantes : DISCORD_BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))  # secondes
COOKIE_TTL = 600  # on renouvelle le cookie toutes les 10 minutes seulement
QUOTA = {"bot_adv": 20, "bot_basic": 3}  # nombre de filtres par palier
EMBED_COLOR = 0xFF6B4A
PERSONAL_CATEGORY_NAME = "🎯・𝗙𝗶𝗹𝘁𝗿𝗲𝘀-𝗽𝗲𝗿𝘀𝗼"
SITE_URL = os.environ.get("SITE_URL", "https://sleeo.com")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

COUNTRY_FLAGS = {"fr": "🇫🇷", "de": "🇩🇪", "be": "🇧🇪", "nl": "🇳🇱", "es": "🇪🇸", "it": "🇮🇹", "pl": "🇵🇱", "pt": "🇵🇹",
                 "at": "🇦🇹", "cz": "🇨🇿", "lt": "🇱🇹", "lu": "🇱🇺", "gb": "🇬🇧"}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

monitors: dict[int, dict] = {}   # channel_id -> {"filters": [ {id, uid, owner, url, api_url, flag, seen:set} ]}
_session: aiohttp.ClientSession | None = None
_cookies: dict[str, tuple[str, float]] = {}

def http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

# ── Supabase ──────────────────────────────────────────────────────────────────
async def sb(method: str, path: str, prefer: str = "return=representation", **kw):
    headers = {"apikey": SUPABASE_KEY, "Content-Type": "application/json", "Prefer": prefer}
    if not SUPABASE_KEY.startswith("sb_"):  # anciennes clés (JWT) : en-tête Authorization nécessaire
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    try:
        async with http().request(method, f"{SUPABASE_URL}/rest/v1/{path}", headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=15), **kw) as r:
            if r.status >= 300:
                print(f"[supabase] {method} {path} -> {r.status} {await r.text()}")
                return None
            if r.status == 204 or prefer.endswith("minimal"):
                return []
            return await r.json(content_type=None)
    except Exception as e:
        print(f"[supabase] erreur : {e}")
        return None

async def account_of(discord_id: int):
    """Retourne {'uid', 'modules'} si le compte Discord est relié à un compte Sleeo, sinon None."""
    rows = await sb("GET", f"profiles?discord_id=eq.{int(discord_id)}&select=id")
    if not rows:
        return None
    uid = rows[0]["id"]
    mods = await sb("POST", "rpc/active_modules", json={"uid": uid}) or []
    return {"uid": uid, "modules": mods}

def quota_of(modules: list[str]) -> int:
    return max([QUOTA[m] for m in modules if m in QUOTA], default=0)

async def load_alerts():
    rows = await sb("GET", "alerts?channel=eq.discord&active=eq.true&select=*") or []
    for a in rows:
        f = a.get("filters") or {}
        try:
            cid = int(a["discord_channel_id"])
        except (TypeError, ValueError):
            continue
        monitors.setdefault(cid, {"filters": []})["filters"].append(
            {"id": a["id"], "uid": a["user_id"], "owner": int(f.get("owner", 0)), "url": a["search_url"],
             "api_url": f.get("api_url") or build_api_url(a["search_url"]), "flag": f.get("flag", "🌍"), "seen": set()})
    print(f"[init] {len(rows)} filtre(s) rechargé(s)")

# ── Vinted ────────────────────────────────────────────────────────────────────
def headers(cookie: str, base: str) -> dict:
    return {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "fr-FR,fr;q=0.9",
            "Referer": base + "/", "Cookie": cookie}

async def get_cookie(base: str) -> str:
    cached = _cookies.get(base)
    if cached and time.time() - cached[1] < COOKIE_TTL:
        return cached[0]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(base + "/", headers={"User-Agent": UA, "Accept": "text/html"},
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                jar = s.cookie_jar.filter_cookies(URL(base + "/"))
                cookie = "; ".join(f"{k}={v.value}" for k, v in jar.items())
                _cookies[base] = (cookie, time.time())
                return cookie
    except Exception as e:
        print(f"[cookie] {e}")
        return cached[0] if cached else ""

async def vget(url: str, base: str):
    cookie = await get_cookie(base)
    try:
        async with http().get(url, headers=headers(cookie, base), timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status in (401, 403):
                _cookies.pop(base, None)  # le cookie est périmé : on le renouvelle au prochain tour
            if r.status != 200:
                print(f"[vinted] statut {r.status} pour {url[:80]}")
                return None
            return await r.json(content_type=None)
    except Exception as e:
        print(f"[vinted] {e}")
        return None

def base_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"

def build_api_url(filter_url: str) -> str:
    p = urlparse(filter_url)
    params = {k: v[0] for k, v in parse_qs(p.query, keep_blank_values=True).items()}
    params.update({"per_page": "20", "order": "newest_first"})
    return f"{base_of(filter_url)}/api/v2/catalog/items?{urlencode(params)}"

def flag_of(url: str) -> str:
    m = re.search(r"vinted\.(\w+)", url)
    return COUNTRY_FLAGS.get(m.group(1) if m else "fr", "🌍")

# ── Mise en forme ─────────────────────────────────────────────────────────────
def price_str(raw) -> str:
    try:
        if isinstance(raw, dict):
            return f"**{float(str(raw.get('amount') or raw.get('amount_rounded'))):.2f} {raw.get('currency_code', 'EUR')}**"
        return f"**{float(str(raw).replace(',', '.')):.2f} €**"
    except Exception:
        return f"**{raw}**"

def stars(rep, count) -> str:
    try:
        n = int(count or 0)
        if n == 0 or rep is None:
            return "Aucun avis (0)"
        rep = float(rep)
        if rep > 5:
            rep /= 100
        k = max(1, min(5, round(rep * 5))) if rep else 0
        return ("⭐" * k + "☆" * (5 - k) + f" ({n})") if k else f"Avis présents ({n})"
    except Exception:
        return "Aucun avis (0)"

COND = {"6": "Neuf avec étiquette", "5": "Neuf sans étiquette", "4": "Très bon état", "3": "Bon état", "2": "Satisfaisant"}

def condition(item: dict, detail: dict) -> str:
    for src in (detail, item):
        sid = str(src.get("status_id") or "")
        if sid in COND:
            return COND[sid]
        s = src.get("status")
        if isinstance(s, dict) and (s.get("title") or s.get("name")):
            return str(s.get("title") or s.get("name"))
        if isinstance(s, str) and s:
            return s
    return "—"

class ArticleView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Voir l'annonce", style=discord.ButtonStyle.link, url=url, emoji="🔗"))
        self.add_item(discord.ui.Button(label="Faire une offre", style=discord.ButtonStyle.link, url=url, emoji="💬"))

async def send_item(channel, item: dict, detail: dict, flag: str):
    url = item.get("url", "")
    u = detail.get("user") or item.get("user") or {}
    photo = u.get("photo") or {}
    e = discord.Embed(title=f"{flag} {item.get('title', 'Sans titre')}", url=url, color=EMBED_COLOR)
    e.set_author(name=u.get("login", "Inconnu"), url=f"https://www.vinted.fr/member/{u.get('id', '')}",
                 icon_url=photo.get("url") if isinstance(photo, dict) else None)
    e.add_field(name="📅 Mis à jour", value=f"<t:{int(time.time())}:R>")
    e.add_field(name="👕 Taille", value=detail.get("size_title") or item.get("size_title") or "—")
    e.add_field(name="🏷️ Marque", value=detail.get("brand_title") or item.get("brand_title") or "—")
    e.add_field(name="🧼 État", value=condition(item, detail))
    e.add_field(name="⭐ Note", value=stars(u.get("feedback_reputation") or u.get("positive_feedback_share"),
                                          u.get("feedback_count") or u.get("ratings_count")))
    e.add_field(name="💰 Prix", value=price_str(detail.get("price") or item.get("price") or "?"))
    photos = [p.get("full_size_url") or p.get("url", "") for p in (detail.get("photos") or item.get("photos") or [])[:3]]
    photos = [p for p in photos if p]
    if photos:
        e.set_image(url=photos[0])
    e.set_footer(text="Sleeo Watch")
    embeds = [e] + [discord.Embed(url=url, color=EMBED_COLOR).set_image(url=p) for p in photos[1:3]]
    await channel.send(embeds=embeds, view=ArticleView(url))

# ── Interface Discord ─────────────────────────────────────────────────────────
NO_ACCOUNT = (f"🔗 Relie d'abord ton compte Discord à ton compte Sleeo : **{SITE_URL}** > Mon compte > Connecter Discord.")

def my_filters(user_id: int):
    return [(cid, f) for cid, d in monitors.items() for f in d["filters"] if f["owner"] == user_id]

class FiltreModal(discord.ui.Modal, title="🛍️ Ajouter un filtre Vinted"):
    url_input = discord.ui.TextInput(label="Lien filtre Vinted", placeholder="https://www.vinted.fr/catalog?search_text=...",
                                     style=discord.TextStyle.long, max_length=500)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        url = self.url_input.value.strip()
        if not re.match(r"^https://www\.vinted\.[a-z.]+/", url):
            return await i.followup.send("❌ URL invalide : colle un lien https://www.vinted.xx/...", ephemeral=True)
        acc = await account_of(i.user.id)
        if not acc:
            return await i.followup.send(NO_ACCOUNT, ephemeral=True)
        limit = quota_of(acc["modules"])
        if limit == 0:
            return await i.followup.send(f"🔒 Il te faut le module Bot. Voir les offres : {SITE_URL}", ephemeral=True)
        if len(my_filters(i.user.id)) >= limit:
            return await i.followup.send(f"⚠️ Limite atteinte : {limit} filtres pour ton palier.", ephemeral=True)
        flt = {"owner": str(i.user.id), "api_url": build_api_url(url), "flag": flag_of(url)}
        rows = await sb("POST", "alerts", json={"user_id": acc["uid"], "nom": f"Discord {i.channel_id}", "search_url": url,
                                                "filters": flt, "channel": "discord",
                                                "discord_channel_id": str(i.channel_id), "active": True})
        if not rows:
            return await i.followup.send("❌ Enregistrement impossible, réessaie plus tard.", ephemeral=True)
        monitors.setdefault(i.channel_id, {"filters": []})["filters"].append(
            {"id": rows[0]["id"], "uid": acc["uid"], "owner": i.user.id, "url": url, "api_url": flt["api_url"],
             "flag": flt["flag"], "seen": set()})
        await i.followup.send(f"✅ Filtre ajouté ({len(my_filters(i.user.id))}/{limit}). Vérification toutes les {CHECK_INTERVAL}s.", ephemeral=True)

async def stop_mine(i: discord.Interaction, whole_channel_if_admin: bool = False):
    admin = i.user.guild_permissions.administrator if i.guild else False
    d = monitors.get(i.channel_id)
    if not d:
        return await i.response.send_message("ℹ️ Aucune surveillance active ici.", ephemeral=True)
    mine = [f for f in d["filters"] if f["owner"] == i.user.id or (admin and whole_channel_if_admin)]
    if not mine:
        return await i.response.send_message("ℹ️ Tu n'as aucun filtre actif dans ce salon.", ephemeral=True)
    for f in mine:
        await sb("PATCH", f"alerts?id=eq.{f['id']}", json={"active": False})
        d["filters"].remove(f)
    if not d["filters"]:
        monitors.pop(i.channel_id, None)
    await i.response.send_message(f"✅ {len(mine)} filtre(s) arrêté(s).", ephemeral=True)

class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ajouter un filtre", style=discord.ButtonStyle.success, custom_id="btn_add_filter", emoji="➕")
    async def add(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(FiltreModal())

    @discord.ui.button(label="Arrêter mes filtres ici", style=discord.ButtonStyle.danger, custom_id="btn_stop_filter", emoji="🛑")
    async def stop(self, i: discord.Interaction, b: discord.ui.Button):
        await stop_mine(i)

    @discord.ui.button(label="Mes surveillances", style=discord.ButtonStyle.secondary, custom_id="btn_list_filters", emoji="📋")
    async def lst(self, i: discord.Interaction, b: discord.ui.Button):
        mine = my_filters(i.user.id)
        txt = "\n".join(f"• <#{cid}> : {f['url'][:70]}" for cid, f in mine) or "ℹ️ Aucune surveillance active."
        await i.response.send_message("**Tes surveillances :**\n" + txt, ephemeral=True)

class PersonalView(PanelView):
    pass

class CreatePersonalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Créer mon filtre perso", style=discord.ButtonStyle.success, custom_id="btn_create_personal", emoji="🔒")
    async def create(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        if not await account_of(i.user.id):
            return await i.followup.send(NO_ACCOUNT, ephemeral=True)
        cat = discord.utils.get(i.guild.categories, name=PERSONAL_CATEGORY_NAME)
        if cat is None:
            return await i.followup.send(f"❌ Catégorie `{PERSONAL_CATEGORY_NAME}` introuvable.", ephemeral=True)
        tag = f"sleeo-owner:{i.user.id}"
        existing = next((c for c in cat.text_channels if (c.topic or "").startswith(tag)), None)
        if existing:
            return await i.followup.send(f"Tu as déjà un salon perso : {existing.mention}", ephemeral=True)
        ow = {i.guild.default_role: discord.PermissionOverwrite(view_channel=False),
              i.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
              i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)}
        name = re.sub(r"[^a-z0-9-]", "-", i.user.display_name.lower())[:20] or f"membre-{i.user.id}"
        ch = await i.guild.create_text_channel(f"filtre-{name}", category=cat, overwrites=ow, topic=f"{tag} · salon privé")
        await ch.send(f"🔒 Bienvenue {i.user.mention}, ce salon n'est visible que par toi. Ajoute tes filtres avec les boutons.", view=PanelView())
        await i.followup.send(f"✅ Ton salon est prêt : {ch.mention}", ephemeral=True)

@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup(ctx):
    e = discord.Embed(title="🛍️ Sleeo Watch, assistant de veille Vinted", color=EMBED_COLOR, description=(
        "Surveille des annonces Vinted publiques et t'alerte en temps réel.\n\n"
        "1️⃣ Va sur **vinted.fr** et applique tes filtres\n2️⃣ Copie l'URL de la barre d'adresse\n"
        "3️⃣ Clique **➕ Ajouter un filtre** et colle l'URL\n\n"
        f"⏱️ Vérification toutes les **{CHECK_INTERVAL} secondes**. Compte Sleeo requis : {SITE_URL}"))
    await ctx.channel.send(embed=e, view=PanelView())

@bot.command(name="filtreperso")
@commands.has_permissions(administrator=True)
async def filtreperso(ctx):
    e = discord.Embed(title="🔒 Ton filtre Vinted personnel", color=EMBED_COLOR,
                      description="Crée ton salon privé pour configurer tes filtres rien qu'à toi.")
    await ctx.channel.send(embed=e, view=CreatePersonalView())

# ── Boucle de veille ──────────────────────────────────────────────────────────
TEST_SEND = os.environ.get("TEST_SEND") == "1"

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_vinted():
    nb = sum(len(d["filters"]) for d in monitors.values())
    print(f"[tick] salons={len(monitors)} filtres={nb}")
    for cid, d in list(monitors.items()):
        channel = bot.get_channel(cid)
        if not channel:
            print(f"[tick] salon {cid} introuvable (le bot n'y a pas accès ?)")
            continue
        for f in list(d["filters"]):
            data = await vget(f["api_url"], base_of(f["url"]))
            items = (data or {}).get("items", [])
            print(f"[tick] {f['url'][:60]} -> {len(items)} annonces (réponse reçue : {data is not None})")
            if not items:
                continue
            if not f["seen"]:  # première lecture : on mémorise sans envoyer
                f["seen"].update(x["id"] for x in items)
                if TEST_SEND:  # mode test : envoie la plus récente pour vérifier
                    det = (await vget(f"{base_of(f['url'])}/api/v2/items/{items[0]['id']}", base_of(f["url"]))) or {}
                    await send_item(channel, items[0], det.get("item", {}), f["flag"])
                continue
            for item in reversed([x for x in items if x["id"] not in f["seen"]]):
                f["seen"].add(item["id"])
                det = (await vget(f"{base_of(f['url'])}/api/v2/items/{item['id']}", base_of(f["url"]))) or {}
                await send_item(channel, item, det.get("item", {}), f["flag"])
                await asyncio.sleep(1)
            if len(f["seen"]) > 500:
                f["seen"] = set(list(f["seen"])[-200:])

@check_vinted.before_loop
async def before_check():
    await bot.wait_until_ready()


# ── Bot sur le site : alertes "web" -> table alert_hits ───────────────────────
web_seen: dict[str, set] = {}
_quota_cache: dict[str, tuple[int, float]] = {}

async def quota_for_uid(uid: str) -> int:
    c = _quota_cache.get(uid)
    if c and time.time() - c[1] < 300:
        return c[0]
    mods = await sb("POST", "rpc/active_modules", json={"uid": uid}) or []
    q = quota_of(mods)
    _quota_cache[uid] = (q, time.time())
    return q

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_web_alerts():
    rows = await sb("GET", "alerts?channel=eq.web&active=eq.true&select=*&order=created_at.asc") or []
    per_user: dict[str, int] = {}
    for a in rows:
        uid, aid = a["user_id"], a["id"]
        per_user[uid] = per_user.get(uid, 0) + 1
        if per_user[uid] > await quota_for_uid(uid):  # au-delà du quota du palier : ignoré
            continue
        url = a["search_url"]
        if not re.match(r"^https://www\.vinted\.[a-z.]+/", url):
            continue
        data = await vget(build_api_url(url), base_of(url))
        items = (data or {}).get("items", [])
        if not items:
            continue
        seen = web_seen.setdefault(aid, set())
        if not seen:  # première lecture : on mémorise sans enregistrer
            seen.update(x["id"] for x in items)
            continue
        for it in reversed([x for x in items if x["id"] not in seen]):
            seen.add(it["id"])
            p = it.get("price")
            amount = p.get("amount") if isinstance(p, dict) else p
            try:
                amount = float(str(amount).replace(",", "."))
            except (TypeError, ValueError):
                amount = None
            photo = it.get("photo") or {}
            await sb("POST", "alert_hits?on_conflict=alert_id,item_id", prefer="resolution=ignore-duplicates,return=minimal",
                     json={"user_id": uid, "alert_id": aid, "item_id": it["id"], "title": it.get("title"),
                           "price": amount, "currency": (p.get("currency_code") if isinstance(p, dict) else "EUR"),
                           "brand": it.get("brand_title"), "size": it.get("size_title"), "url": it.get("url"),
                           "image": photo.get("url") if isinstance(photo, dict) else None})
        if len(seen) > 500:
            web_seen[aid] = set(list(seen)[-200:])

@check_web_alerts.before_loop
async def before_web():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"✅ Connecté : {bot.user}")
    if not check_vinted.is_running():
        await load_alerts()
        bot.add_view(PanelView())
        bot.add_view(CreatePersonalView())
        check_vinted.start()
        check_web_alerts.start()

bot.run(BOT_TOKEN)
