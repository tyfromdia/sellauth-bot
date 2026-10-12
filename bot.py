# bot.py
import os
from dotenv import load_dotenv
load_dotenv()
import hmac
import hashlib
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web, ClientSession
from datetime import datetime, timezone

TOKEN                    = os.getenv("DISCORD_TOKEN")
SALES_CHANNEL_ID         = int(os.getenv("SALES_CHANNEL_ID", "0"))
SELLAUTH_API_KEY         = os.getenv("SELLAUTH_API_KEY")
SELLAUTH_SHOP_ID         = os.getenv("SELLAUTH_SHOP_ID")
SELLAUTH_WEBHOOK_SECRET  = os.getenv("SELLAUTH_WEBHOOK_SECRET")
WEBHOOK_PORT             = int(os.getenv("WEBHOOK_PORT", "8080"))

API_BASE = "https://api.sellauth.com/v1"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

def make_embed(title: str, color: discord.Color) -> discord.Embed:
    emb = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    emb.set_footer(text="SellAuth")
    return emb

async def api_get(path: str) -> dict | None:
    headers = {"Authorization": f"Bearer {SELLAUTH_API_KEY}"}
    async with ClientSession() as session:
        async with session.get(f"{API_BASE}{path}", headers=headers) as resp:
            if resp.status != 200:
                print(f"API {path} → {resp.status}")
                return None
            return await resp.json()

def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    if not signature or not SELLAUTH_WEBHOOK_SECRET:
        return False
    expected = hmac.new(
        SELLAUTH_WEBHOOK_SECRET.encode(),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

# ── Manual commands ────────────────────────────────────────────────
@tree.command(name="sale", description="Manually announce a sale")
@app_commands.describe(item="Product", price="Price", buyer="Buyer (optional)", notes="Notes (optional)")
async def sale_cmd(interaction: discord.Interaction, item: str, price: str, buyer: str = None, notes: str = None):
    channel = bot.get_channel(SALES_CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("Sales channel missing.", ephemeral=True)
        return
    emb = make_embed("💰 New Sale", discord.Color.green())
    emb.add_field(name="Product", value=item, inline=True)
    emb.add_field(name="Price", value=price, inline=True)
    if buyer:
        emb.add_field(name="Buyer", value=buyer, inline=True)
    if notes:
        emb.add_field(name="Notes", value=notes, inline=False)
    await channel.send(embed=emb)
    await interaction.response.send_message("Posted.", ephemeral=True)

@tree.command(name="restock", description="Manually announce a restock")
@app_commands.describe(item="Product", quantity="Qty (optional)", notes="Notes (optional)")
async def restock_cmd(interaction: discord.Interaction, item: str, quantity: str = None, notes: str = None):
    channel = bot.get_channel(SALES_CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("Sales channel missing.", ephemeral=True)
        return
    emb = make_embed("📦 Restock", discord.Color.blue())
    emb.add_field(name="Product", value=item, inline=True)
    if quantity:
        emb.add_field(name="Quantity", value=quantity, inline=True)
    if notes:
        emb.add_field(name="Notes", value=notes, inline=False)
    await channel.send(embed=emb)
    await interaction.response.send_message("Posted.", ephemeral=True)

# ── SellAuth webhook ───────────────────────────────────────────────
async def handle_sellauth(request: web.Request):
    raw = await request.read()
    sig = request.headers.get("X-Signature")

    if not verify_signature(raw, sig):
        return web.Response(status=401, text="invalid signature")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    event = payload.get("event")
    data  = payload.get("data") or {}
    channel = bot.get_channel(SALES_CHANNEL_ID)
    if not channel:
        return web.Response(status=500, text="channel missing")

    # ── Sale ───────────────────────────────────────────────────────
    if event == "NOTIFICATION.SHOP_INVOICE_PROCESSED":
        invoice_id = data.get("invoice_id")
        if not invoice_id:
            return web.Response(status=400, text="missing invoice_id")

        inv = await api_get(f"/shops/{SELLAUTH_SHOP_ID}/invoices/{invoice_id}")
        if not inv:
            return web.Response(status=502, text="invoice fetch failed")

        # Header fields
        total      = inv.get("paid") or inv.get("price") or "?"
        currency   = inv.get("currency") or "USD"
        email      = inv.get("email") or "—"
        gateway    = (inv.get("payment_method") or {}).get("name") or inv.get("gateway") or "—"
        customer   = inv.get("customer") or {}
        discord_id = customer.get("discord_id")
        discord_un = customer.get("discord_username")
        if discord_id and discord_un:
            discord_str = f"{discord_un} (`{discord_id}`)"
        elif discord_id:
            discord_str = f"`{discord_id}`"
        elif discord_un:
            discord_str = discord_un
        else:
            discord_str = "—"

        emb = make_embed("💰 New Sale", discord.Color.green())
        emb.add_field(name="Invoice ID",        value=f"`{invoice_id}`", inline=True)
        emb.add_field(name="Payment Method",    value=gateway, inline=True)
        emb.add_field(name="Total Price",       value=f"`{total} {currency}`", inline=True)
        emb.add_field(name="Customer's E-mail", value=f"`{email}`", inline=True)
        emb.add_field(name="Customer's Discord", value=discord_str, inline=True)
        emb.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

        # Per-item block
        items = inv.get("items") or []
        for idx, it in enumerate(items, start=1):
            product_name = (it.get("product") or {}).get("name") or "Unknown"
            variant_name = (it.get("variant") or {}).get("name")
            if variant_name:
                product_name = f"{product_name} — {variant_name}"

            price = it.get("total_price") or it.get("price") or "?"
            stock = (it.get("variant") or {}).get("stock")
            if stock is None:
                stock_str = "—"
            elif stock == -1:
                stock_str = "∞"
            else:
                stock_str = str(stock)

            emb.add_field(name=f"Item {idx}", value="\u200b", inline=False)
            emb.add_field(name="Product", value=product_name, inline=True)
            emb.add_field(name="Price", value=f"`{price} {currency}`", inline=True)
            emb.add_field(name="Remaining Variant Stock", value=stock_str, inline=True)

        await channel.send(embed=emb)
        return web.Response(text="ok")

    # ── Restock ────────────────────────────────────────────────────
    if event == "NOTIFICATION.SHOP_PRODUCT_RESTOCKED":
        product_id = data.get("product_id")
        if not product_id:
            return web.Response(status=400, text="missing product_id")

        prod = await api_get(f"/shops/{SELLAUTH_SHOP_ID}/products/{product_id}")
        if not prod:
            return web.Response(status=502, text="product fetch failed")

        name = prod.get("name") or "Unknown"
        variants = prod.get("variants") or []
        restocked_ids = set(data.get("restocked_variant_ids") or [])

        emb = make_embed("📦 Restock", discord.Color.blue())
        emb.add_field(name="Product", value=name, inline=False)

        for v in variants:
            if restocked_ids and v.get("id") not in restocked_ids:
                continue
            stock = v.get("stock")
            stock_str = "∞" if stock == -1 else str(stock) if stock is not None else "—"
            emb.add_field(name=v.get("name") or "Variant", value=f"Stock: `{stock_str}`", inline=True)

        if not emb.fields or len(emb.fields) == 1:
            stock = prod.get("stock_count")
            stock_str = "∞" if stock == -1 else str(stock) if stock is not None else "—"
            emb.add_field(name="Stock", value=stock_str, inline=True)

        await channel.send(embed=emb)
        return web.Response(text="ok")

    return web.Response(text="ignored")

# ── Startup ────────────────────────────────────────────────────────
async def start_web():
    app = web.Application()
    app.router.add_post("/sellauth", handle_sellauth)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"SellAuth webhook on :{WEBHOOK_PORT}/sellauth")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        await tree.sync()
    except Exception as e:
        print(f"Sync error: {e}")

@bot.event
async def setup_hook():
    bot.loop.create_task(start_web())

if __name__ == "__main__":
    required = {
        "DISCORD_TOKEN": TOKEN,
        "SALES_CHANNEL_ID": SALES_CHANNEL_ID,
        "SELLAUTH_API_KEY": SELLAUTH_API_KEY,
        "SELLAUTH_SHOP_ID": SELLAUTH_SHOP_ID,
        "SELLAUTH_WEBHOOK_SECRET": SELLAUTH_WEBHOOK_SECRET,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print("Missing env:", ", ".join(missing))
        exit(1)
    bot.run(TOKEN)