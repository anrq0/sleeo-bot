"""Sleeo Watch v2 : bot Discord relié aux comptes Sleeo (Supabase).
La veille de Vinted est faite par l'EXTENSION SleeOs du membre (dans son navigateur). Ce bot :
  - permet de créer / lister / supprimer ses filtres (quota selon bot_basic = 3, bot_adv = 20)
  - poste dans Discord les annonces trouvées (table alert_hits) dans le salon choisi
Variables : DISCORD_BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY (SECRÈTE, seulement sur le serveur du bot), SITE_URL
"""
import os, re, asyncio
import aiohttp, discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

try:
    from keep_alive import keep_alive  # facultatif (Render en "Web Service")
    keep_alive()
except ImportError:
    pass

load_dotenv()
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
SB_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SITE_URL = os.environ.get("SITE_URL", "https://sleeo.lovable.app")
if not (TOKEN and SB_URL and SB_KEY):
    raise RuntimeError("Variables manquantes : DISCORD_BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY")

COLOR = 0xFF6B4A
QUOTA = {"bot_adv": 20, "bot_basic": 3}
URL_RE = re.compile(r"^https://www\.vinted\.[a-z.]+/catalog")
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
_session = None

def http():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

async def sb(method, path, prefer="return=representation", **kw):
    headers = {"apikey": SB_KEY, "Content-Type": "application/json", "Prefer": prefer}
    if not SB_KEY.startswith("sb_"):
        headers["Authorization"] = f"Bearer {SB_KEY}"
    try:
        async with http().request(method, f"{SB_URL}/rest/v1/{path}", headers=headers, timeout=aiohttp.ClientTimeout(total=20), **kw) as r:
            if r.status >= 300:
                print(f"[supabase] {method} {path} -> {r.status} {await r.text()}")
                return None
            return [] if (r.status == 204 or prefer.endswith("minimal")) else await r.json(content_type=None)
    except Exception as e:
        print(f"[supabase] erreur : {e}")
        return None

async def account_of(discord_id: int):
    rows = await sb("GET", f"profiles?discord_id=eq.{int(discord_id)}&select=id")
    if not rows:
        return None
    mods = await sb("POST", "rpc/active_modules", json={"uid": rows[0]["id"]}) or []
    return {"uid": rows[0]["id"], "quota": max([QUOTA[m] for m in mods if m in QUOTA], default=0), "adv": "bot_adv" in mods}

NO_ACCOUNT = f"🔗 Relie d'abord ton compte Discord à ton compte Sleeo : **{SITE_URL}** > Mon compte > Connecter Discord."
NOTE = ("ℹ️ La veille tourne dans ton **extension SleeOs** (Chrome ouvert, avec un onglet vinted.fr connecté). "
        "Les annonces arrivent ensuite ici.")

class FilterModal(discord.ui.Modal, title="🛍️ Ajouter un filtre Vinted"):
    name = discord.ui.TextInput(label="Nom du filtre", placeholder="Carhartt vestes", required=False, max_length=60)
    url = discord.ui.TextInput(label="Lien de recherche Vinted", placeholder="https://www.vinted.fr/catalog?search_text=...", style=discord.TextStyle.long, max_length=500)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        link = self.url.value.strip()
        if not URL_RE.match(link):
            return await i.followup.send("❌ Colle un lien de recherche `https://www.vinted.xx/catalog?...`.", ephemeral=True)
        acc = await account_of(i.user.id)
        if not acc:
            return await i.followup.send(NO_ACCOUNT, ephemeral=True)
        if acc["quota"] == 0:
            return await i.followup.send(f"🔒 Il te faut le module Bot. Offres : {SITE_URL}", ephemeral=True)
        mine = await sb("GET", f"alerts?user_id=eq.{acc['uid']}&active=eq.true&select=id") or []
        if len(mine) >= acc["quota"]:
            return await i.followup.send(f"⚠️ Limite atteinte : {acc['quota']} filtres pour ton palier.", ephemeral=True)
        row = await sb("POST", "alerts", json={"user_id": acc["uid"], "nom": self.name.value.strip() or link[:60], "search_url": link,
                                               "channel": "discord", "discord_channel_id": str(i.channel_id), "active": True})
        if not row:
            return await i.followup.send("❌ Enregistrement impossible, réessaie.", ephemeral=True)
        await i.followup.send(f"✅ Filtre ajouté ({len(mine) + 1}/{acc['quota']}).\n{NOTE}", ephemeral=True)

class DeleteSelect(discord.ui.Select):
    def __init__(self, rows):
        super().__init__(placeholder="Choisis le filtre à supprimer",
                         options=[discord.SelectOption(label=(r["nom"] or r["search_url"])[:90], value=r["id"]) for r in rows[:25]])

    async def callback(self, i: discord.Interaction):
        await sb("PATCH", f"alerts?id=eq.{self.values[0]}", json={"active": False})
        await i.response.edit_message(content="✅ Filtre supprimé.", view=None)

class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ajouter un filtre", style=discord.ButtonStyle.success, custom_id="sl_add", emoji="➕")
    async def add(self, i: discord.Interaction, b):
        await i.response.send_modal(FilterModal())

    @discord.ui.button(label="Mes filtres", style=discord.ButtonStyle.secondary, custom_id="sl_list", emoji="📋")
    async def lst(self, i: discord.Interaction, b):
        acc = await account_of(i.user.id)
        if not acc:
            return await i.response.send_message(NO_ACCOUNT, ephemeral=True)
        rows = await sb("GET", f"alerts?user_id=eq.{acc['uid']}&active=eq.true&select=nom,search_url,discord_channel_id,last_scan&order=created_at.asc") or []
        txt = "\n".join(f"• **{r['nom']}** → <#{r['discord_channel_id']}>" if r.get("discord_channel_id") else f"• **{r['nom']}** (site)" for r in rows)
        await i.response.send_message((txt or "Aucun filtre actif.") + f"\n\nQuota : {len(rows)}/{acc['quota']}", ephemeral=True)

    @discord.ui.button(label="Supprimer un filtre", style=discord.ButtonStyle.danger, custom_id="sl_del", emoji="🗑️")
    async def delete(self, i: discord.Interaction, b):
        acc = await account_of(i.user.id)
        if not acc:
            return await i.response.send_message(NO_ACCOUNT, ephemeral=True)
        rows = await sb("GET", f"alerts?user_id=eq.{acc['uid']}&active=eq.true&select=id,nom,search_url&order=created_at.asc") or []
        if not rows:
            return await i.response.send_message("Aucun filtre actif.", ephemeral=True)
        v = discord.ui.View(timeout=60); v.add_item(DeleteSelect(rows))
        await i.response.send_message("Quel filtre supprimer ?", view=v, ephemeral=True)

class ItemView(discord.ui.View):
    def __init__(self, url):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Voir l'annonce", style=discord.ButtonStyle.link, url=url, emoji="🔗"))

@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup(ctx):
    e = discord.Embed(title="🛍️ Sleeo Watch, veille Vinted", color=COLOR, description=(
        "Reçois ici les nouvelles annonces de tes recherches Vinted.\n\n1️⃣ Sur vinted.fr, applique tes filtres\n2️⃣ Copie le lien de la barre d'adresse\n"
        "3️⃣ Clique **➕ Ajouter un filtre**\n\n" + NOTE + f"\nCompte Sleeo requis : {SITE_URL}"))
    await ctx.channel.send(embed=e, view=PanelView())

@tasks.loop(seconds=15)
async def post_hits():
    rows = await sb("GET", "alert_hits?posted=eq.false&select=id,title,price,currency,brand,size,etat,favoris,url,image,alerts(nom,channel,discord_channel_id)&order=seen_at.asc&limit=15") or []
    for h in rows:
        a = h.get("alerts") or {}
        ok = True
        try:
            ch = bot.get_channel(int(a["discord_channel_id"])) if a.get("channel") == "discord" and a.get("discord_channel_id") else None
            if ch and h.get("url"):
                e = discord.Embed(title=h.get("title") or "Nouvelle annonce", url=h["url"], color=COLOR)
                e.add_field(name="💰 Prix", value=f"**{h['price']:.2f} €**" if h.get("price") is not None else "—")
                e.add_field(name="🏷️ Marque", value=h.get("brand") or "—")
                e.add_field(name="👕 Taille", value=h.get("size") or "—")
                e.add_field(name="🧼 État", value=h.get("etat") or "—")
                if h.get("favoris") is not None:
                    e.add_field(name="❤️ Favoris", value=str(h["favoris"]))
                if h.get("image"):
                    e.set_image(url=h["image"])
                e.set_footer(text=f"Sleeo Watch · {a.get('nom', '')}")
                await ch.send(embed=e, view=ItemView(h["url"]))
                await asyncio.sleep(1)
        except Exception as ex:
            print(f"[post] {ex}")
            ok = False
        if ok:
            await sb("PATCH", f"alert_hits?id=eq.{h['id']}", json={"posted": True}, prefer="return=minimal")

@post_hits.before_loop
async def _wait():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"✅ Connecté : {bot.user}")
    bot.add_view(PanelView())
    if not post_hits.is_running():
        post_hits.start()

bot.run(TOKEN)
