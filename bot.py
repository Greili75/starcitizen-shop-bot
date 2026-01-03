import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json
import os
from datetime import datetime
import sqlite3

# Bot Setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!sc', intents=intents)

# Konfiguration aus Umgebungsvariablen
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID', 0))
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', 300))  # Standard: 5 Minuten

# Datenbank Setup
def init_db():
    conn = sqlite3.connect('shop_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS ships
                 (id TEXT PRIMARY KEY, 
                  name TEXT, 
                  price REAL, 
                  available INTEGER,
                  last_updated TEXT,
                  data TEXT)''')
    conn.commit()
    conn.close()

# RSI API Endpoints (Community reverse-engineered)
RSI_STORE_API = "https://robertsspaceindustries.com/store/api/graphql"

async def fetch_shop_data():
    """Holt aktuelle Shop-Daten von RSI"""
    
    # GraphQL Query für den Shop
    query = """
    query {
        store {
            listing {
                id
                name
                title
                excerpt
                body
                slug
                url
                store_url
                from_price
                to_price
                price_range
                media {
                    thumbnail {
                        url
                    }
                }
                nativePrice {
                    amount
                    discounted
                }
                availability {
                    listedAt
                    soldOut
                    isLimited
                }
                tags
                type
            }
        }
    }
    """
    
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0'
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            # Versuche die GraphQL API
            async with session.post(
                RSI_STORE_API,
                json={'query': query},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    return await response.json()
                
                # Fallback: Versuche direkte Store API
                async with session.get(
                    'https://robertsspaceindustries.com/api/store/getStoreIndex',
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as fallback_response:
                    if fallback_response.status == 200:
                        return await fallback_response.json()
    except Exception as e:
        print(f"Fehler beim Abrufen der Shop-Daten: {e}")
    
    return None

async def parse_shop_items(data):
    """Parst Shop-Daten in einheitliches Format"""
    items = []
    
    if not data:
        return items
    
    try:
        # Versuche verschiedene API-Strukturen zu parsen
        if 'data' in data and 'store' in data['data']:
            # GraphQL Response
            listings = data['data']['store']['listing']
            for item in listings:
                items.append({
                    'id': item.get('id', ''),
                    'name': item.get('name', item.get('title', 'Unknown')),
                    'price': item.get('nativePrice', {}).get('amount', 0),
                    'discounted': item.get('nativePrice', {}).get('discounted', False),
                    'available': not item.get('availability', {}).get('soldOut', False),
                    'is_limited': item.get('availability', {}).get('isLimited', False),
                    'url': f"https://robertsspaceindustries.com{item.get('store_url', '')}",
                    'thumbnail': item.get('media', {}).get('thumbnail', {}).get('url', ''),
                    'type': item.get('type', 'unknown'),
                    'excerpt': item.get('excerpt', '')
                })
        elif 'success' in data and data['success'] == 1:
            # Direct Store API Response
            for item in data.get('data', {}).get('store', []):
                items.append({
                    'id': str(item.get('id', '')),
                    'name': item.get('name', 'Unknown'),
                    'price': float(item.get('price', 0)),
                    'discounted': item.get('discounted', False),
                    'available': item.get('available', True),
                    'is_limited': item.get('limited', False),
                    'url': item.get('url', ''),
                    'thumbnail': item.get('thumbnail', ''),
                    'type': item.get('type', 'unknown'),
                    'excerpt': item.get('excerpt', '')
                })
    except Exception as e:
        print(f"Fehler beim Parsen der Daten: {e}")
    
    return items

def get_stored_items():
    """Holt gespeicherte Items aus der Datenbank"""
    conn = sqlite3.connect('shop_data.db')
    c = conn.cursor()
    c.execute('SELECT id, name, price, available, data FROM ships')
    items = {}
    for row in c.fetchall():
        items[row[0]] = {
            'name': row[1],
            'price': row[2],
            'available': bool(row[3]),
            'data': json.loads(row[4]) if row[4] else {}
        }
    conn.close()
    return items

def save_items(items):
    """Speichert Items in der Datenbank"""
    conn = sqlite3.connect('shop_data.db')
    c = conn.cursor()
    
    for item in items:
        c.execute('''INSERT OR REPLACE INTO ships 
                     (id, name, price, available, last_updated, data)
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  (item['id'], 
                   item['name'], 
                   item['price'],
                   1 if item['available'] else 0,
                   datetime.now().isoformat(),
                   json.dumps(item)))
    
    conn.commit()
    conn.close()

def create_embed(item, change_type):
    """Erstellt ein Discord Embed für Shop-Updates"""
    
    colors = {
        'new': discord.Color.green(),
        'price_change': discord.Color.gold(),
        'available': discord.Color.blue(),
        'sold_out': discord.Color.red(),
        'limited': discord.Color.purple()
    }
    
    embed = discord.Embed(
        title=item['name'],
        url=item.get('url', ''),
        color=colors.get(change_type, discord.Color.blue()),
        timestamp=datetime.now()
    )
    
    if change_type == 'new':
        embed.description = f"🆕 **Neues Item im Shop!**"
    elif change_type == 'price_change':
        embed.description = f"💰 **Preisänderung!**"
    elif change_type == 'available':
        embed.description = f"✅ **Wieder verfügbar!**"
    elif change_type == 'sold_out':
        embed.description = f"❌ **Ausverkauft!**"
    elif change_type == 'limited':
        embed.description = f"⚠️ **Limited Sale!**"
    
    # Preis
    price_text = f"${item['price']:.2f}"
    if item.get('discounted'):
        price_text = f"~~${item['price']:.2f}~~ **REDUZIERT**"
    embed.add_field(name="Preis", value=price_text, inline=True)
    
    # Verfügbarkeit
    avail_text = "✅ Verfügbar" if item['available'] else "❌ Ausverkauft"
    if item.get('is_limited'):
        avail_text += " (LIMITED)"
    embed.add_field(name="Status", value=avail_text, inline=True)
    
    # Typ
    if item.get('type'):
        embed.add_field(name="Typ", value=item['type'].title(), inline=True)
    
    # Beschreibung
    if item.get('excerpt'):
        embed.add_field(name="Beschreibung", value=item['excerpt'][:200], inline=False)
    
    # Thumbnail
    if item.get('thumbnail'):
        embed.set_thumbnail(url=item['thumbnail'])
    
    embed.set_footer(text="Star Citizen Shop Monitor")
    
    return embed

async def check_for_updates():
    """Prüft auf Shop-Updates und sendet Benachrichtigungen"""
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"Channel {CHANNEL_ID} nicht gefunden!")
        return
    
    print(f"Prüfe Shop-Updates... {datetime.now()}")
    
    # Hole aktuelle Daten
    shop_data = await fetch_shop_data()
    current_items = await parse_shop_items(shop_data)
    
    if not current_items:
        print("Keine Shop-Daten erhalten")
        return
    
    # Hole gespeicherte Daten
    stored_items = get_stored_items()
    
    # Vergleiche und finde Änderungen
    for item in current_items:
        item_id = item['id']
        
        if item_id not in stored_items:
            # Neues Item
            embed = create_embed(item, 'new')
            await channel.send(embed=embed)
            print(f"Neues Item: {item['name']}")
            
        else:
            stored = stored_items[item_id]
            
            # Preisänderung
            if abs(stored['price'] - item['price']) > 0.01:
                item['old_price'] = stored['price']
                embed = create_embed(item, 'price_change')
                embed.add_field(
                    name="Alter Preis", 
                    value=f"${stored['price']:.2f}",
                    inline=True
                )
                await channel.send(embed=embed)
                print(f"Preisänderung: {item['name']}")
            
            # Verfügbarkeitsänderung
            if stored['available'] != item['available']:
                if item['available']:
                    embed = create_embed(item, 'available')
                    await channel.send(embed=embed)
                    print(f"Wieder verfügbar: {item['name']}")
                else:
                    embed = create_embed(item, 'sold_out')
                    await channel.send(embed=embed)
                    print(f"Ausverkauft: {item['name']}")
            
            # Limited Sale
            if item.get('is_limited') and not stored.get('data', {}).get('is_limited'):
                embed = create_embed(item, 'limited')
                await channel.send(embed=embed)
                print(f"Limited Sale: {item['name']}")
    
    # Speichere aktuelle Daten
    save_items(current_items)

@tasks.loop(seconds=CHECK_INTERVAL)
async def monitor_shop():
    """Background Task für Shop-Monitoring"""
    try:
        await check_for_updates()
    except Exception as e:
        print(f"Fehler im Monitor: {e}")

@bot.event
async def on_ready():
    print(f'{bot.user} ist online!')
    print(f'Monitoring Channel ID: {CHANNEL_ID}')
    print(f'Check Interval: {CHECK_INTERVAL} Sekunden')
    
    # Initialisiere Datenbank
    init_db()
    
    # Starte Monitoring
    if not monitor_shop.is_running():
        monitor_shop.start()
    
    print("Shop-Monitoring gestartet!")

@bot.command(name='status')
async def status(ctx):
    """Zeigt Bot-Status"""
    embed = discord.Embed(
        title="Star Citizen Shop Monitor Status",
        color=discord.Color.green()
    )
    embed.add_field(name="Status", value="✅ Online", inline=True)
    embed.add_field(name="Check Interval", value=f"{CHECK_INTERVAL}s", inline=True)
    
    conn = sqlite3.connect('shop_data.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM ships')
    count = c.fetchone()[0]
    conn.close()
    
    embed.add_field(name="Überwachte Items", value=str(count), inline=True)
    await ctx.send(embed=embed)

@bot.command(name='check')
@commands.has_permissions(administrator=True)
async def manual_check(ctx):
    """Manuelle Prüfung auf Updates (nur Admin)"""
    await ctx.send("🔍 Prüfe Shop-Updates...")
    await check_for_updates()
    await ctx.send("✅ Prüfung abgeschlossen!")

# Bot starten
if __name__ == '__main__':
    if not DISCORD_TOKEN:
        print("FEHLER: DISCORD_TOKEN nicht gesetzt!")
    elif not CHANNEL_ID:
        print("FEHLER: CHANNEL_ID nicht gesetzt!")
    else:
        bot.run(DISCORD_TOKEN)
