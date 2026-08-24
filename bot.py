import discord
from discord import app_commands
from discord.ext import commands
import json
import asyncio
import aiohttp
import base58
import struct
import time
import os
import random
from pathlib import Path
from datetime import datetime, timezone
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.instruction import Instruction, AccountMeta
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.system_program import transfer, TransferParams
from solders.hash import Hash

# ─── Constants ───────────────────────────────────────────────────────────────
CONFIG_PATH = Path("config.json")

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
RENT_PROGRAM = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# Pump.fun instruction discriminators
BUY_EXACT_SOL_IN_DISC = bytes([56, 252, 116, 8, 158, 223, 205, 95])
SELL_DISC = bytes([51, 230, 133, 164, 1, 127, 131, 173])

# fee_config PDA key (same for buy AND sell on the bonding curve program)
FEE_CONFIG_KEY = bytes([
    1, 86, 224, 246, 147, 102, 90, 207, 68, 219, 21, 104, 191, 23, 91, 170,
    81, 137, 203, 151, 245, 210, 255, 59, 101, 93, 43, 182, 253, 109, 24, 176,
])

# Derived PDAs (static — computed once at startup)
def _pda(seeds, program):
    pda, _ = Pubkey.find_program_address(seeds, program)
    return pda

GLOBAL_PDA = _pda([b"global"], PUMP_PROGRAM)
EVENT_AUTHORITY = _pda([b"__event_authority"], PUMP_PROGRAM)
GLOBAL_VOLUME_ACCUMULATOR = _pda([b"global_volume_accumulator"], PUMP_PROGRAM)
FEE_CONFIG = _pda([b"fee_config", FEE_CONFIG_KEY], FEE_PROGRAM)

# Fee recipient — one of the 8 Global config recipients. This const is one of
# them and works for buys; the program accepts any configured recipient.
FEE_RECIPIENT = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtRAtymTZ")

JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4bVqkfRtQ7NmXwkihtEtvC",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSLJGfLRWS2yRXFNPCP",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JITO_URL = os.getenv("JITO_URL", "https://mainnet.block-engine.jito.wtf/api/v1/bundles")

# ─── Config ──────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {
        "wallets": [],
        "active_wallets": [],
        "settings": {
            "slippage": 50,
            "jito_tip": 0.001,
            "priority_fee": 200000,
        },
        "snipe_history": [],
    }

def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

# ─── Solana Helpers ──────────────────────────────────────────────────────────

async def get_balance(pubkey: str) -> float:
    async with aiohttp.ClientSession() as session:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [pubkey]}
        async with session.post(RPC_URL, json=payload) as resp:
            data = await resp.json()
            return data["result"]["value"] / 1e9

async def get_token_balance(owner: str, mint: str) -> float:
    async with aiohttp.ClientSession() as session:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        }
        async with session.post(RPC_URL, json=payload) as resp:
            data = await resp.json()
            accounts = data.get("result", {}).get("value", [])
            if accounts:
                return float(accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
            return 0.0

async def get_recent_blockhash() -> str:
    async with aiohttp.ClientSession() as session:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getLatestBlockhash",
            "params": [{"commitment": "finalized"}],
        }
        async with session.post(RPC_URL, json=payload) as resp:
            data = await resp.json()
            return data["result"]["value"]["blockhash"]

async def get_sol_price() -> float:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
            ) as resp:
                data = await resp.json()
                return data["solana"]["usd"]
    except:
        return 0.0

def get_associated_token_address(wallet: Pubkey, mint: Pubkey) -> Pubkey:
    seeds = [bytes(wallet), bytes(TOKEN_PROGRAM), bytes(mint)]
    ata, _ = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM)
    return ata

def get_bonding_curve_address(mint: Pubkey) -> Pubkey:
    return _pda([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)

def get_bonding_curve_v2_address(mint: Pubkey) -> Pubkey:
    # Required on every buy/sell since the Feb 2026 cashback upgrade. Must be
    # the last account. Can be uninitialized on-chain — only used for indexing.
    return _pda([b"bonding-curve-v2", bytes(mint)], PUMP_PROGRAM)

def get_creator_vault_address(creator: Pubkey) -> Pubkey:
    return _pda([b"creator-vault", bytes(creator)], PUMP_PROGRAM)

def get_user_volume_accumulator_address(user: Pubkey) -> Pubkey:
    return _pda([b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM)

def get_ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM) -> Pubkey:
    return _pda([bytes(owner), bytes(token_program), bytes(mint)], ASSOCIATED_TOKEN_PROGRAM)

def get_wallet_pubkeys(config) -> list[str]:
    pubkeys = []
    for pk in config["wallets"]:
        kp = Keypair.from_base58_string(pk)
        pubkeys.append(str(kp.pubkey()))
    return pubkeys

async def get_mint_owner(mint: str) -> Pubkey:
    """Return the token program that owns the mint (classic SPL or Token-2022)."""
    async with aiohttp.ClientSession() as session:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [mint, {"encoding": "base64"}],
        }
        async with session.post(RPC_URL, json=payload) as resp:
            data = await resp.json()
            result = data.get("result", {}).get("value")
            if not result:
                return TOKEN_PROGRAM
            owner = result.get("owner", "")
            try:
                return Pubkey.from_string(owner)
            except:
                return TOKEN_PROGRAM

async def get_bonding_curve_state(bonding_curve: str) -> dict | None:
    """Fetch bonding curve account data: reserves, creator, and cashback flag.

    Layout (151 bytes after the Feb 2026 cashback upgrade):
      0   disc (8)
      8   virtual_token_reserves u64
      16  virtual_sol_reserves   u64
      24  real_token_reserves    u64
      32  real_sol_reserves      u64
      40  token_total_supply     u64
      48  complete               bool
      49  creator                Pubkey (32)
      81  reserved               u8
      82  cashback_enabled       bool
    """
    async with aiohttp.ClientSession() as session:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getAccountInfo",
            "params": [bonding_curve, {"encoding": "base64"}],
        }
        async with session.post(RPC_URL, json=payload) as resp:
            data = await resp.json()
            result = data.get("result", {}).get("value")
            if not result:
                return None
            import base64
            raw = base64.b64decode(result["data"][0])
            if len(raw) < 49:
                return None
            virtual_token = struct.unpack_from("<Q", raw, 8)[0]
            virtual_sol = struct.unpack_from("<Q", raw, 16)[0]
            real_token = struct.unpack_from("<Q", raw, 24)[0]
            real_sol = struct.unpack_from("<Q", raw, 32)[0]
            creator = None
            if len(raw) >= 81:
                creator = Pubkey.from_bytes(raw[49:81])
            cashback_enabled = len(raw) > 82 and raw[82] != 0
            return {
                "virtual_token_reserves": virtual_token,
                "virtual_sol_reserves": virtual_sol,
                "real_token_reserves": real_token,
                "real_sol_reserves": real_sol,
                "creator": creator,
                "cashback_enabled": cashback_enabled,
            }

def calculate_tokens_out(sol_amount_lamports: int, curve_state: dict) -> int:
    """Calculate how many tokens you get for a given SOL amount using the constant product formula."""
    virtual_sol = curve_state["virtual_sol_reserves"]
    virtual_token = curve_state["virtual_token_reserves"]
    # constant product: (virtual_sol + sol_in) * (virtual_token - tokens_out) = virtual_sol * virtual_token
    # tokens_out = virtual_token - (virtual_sol * virtual_token) / (virtual_sol + sol_in)
    # simplified: tokens_out = (sol_in * virtual_token) / (virtual_sol + sol_in)
    tokens_out = (sol_amount_lamports * virtual_token) // (virtual_sol + sol_amount_lamports)
    return tokens_out

# ─── Transaction Building ───────────────────────────────────────────────────

def build_buy_ix(
    mint: Pubkey, buyer: Pubkey, creator: Pubkey,
    sol_amount: int, min_tokens_out: int,
    token_program: Pubkey = TOKEN_PROGRAM,
    fee_recipient: Pubkey = FEE_RECIPIENT,
) -> Instruction:
    """buy_exact_sol_in — 17 accounts. Pins SOL spent, floors tokens received.
    Correct layout as of the Feb 2026 cashback upgrade (bonding_curve_v2 last)."""
    bonding_curve = get_bonding_curve_address(mint)
    bonding_curve_ata = get_ata(bonding_curve, mint, token_program)
    buyer_ata = get_ata(buyer, mint, token_program)
    creator_vault = get_creator_vault_address(creator)
    user_vol = get_user_volume_accumulator_address(buyer)
    bonding_curve_v2 = get_bonding_curve_v2_address(mint)

    data = BUY_EXACT_SOL_IN_DISC + struct.pack("<QQ", sol_amount, min_tokens_out)
    accounts = [
        AccountMeta(GLOBAL_PDA, is_signer=False, is_writable=False),               # 0
        AccountMeta(fee_recipient, is_signer=False, is_writable=True),             # 1
        AccountMeta(mint, is_signer=False, is_writable=False),                     # 2
        AccountMeta(bonding_curve, is_signer=False, is_writable=True),             # 3
        AccountMeta(bonding_curve_ata, is_signer=False, is_writable=True),         # 4
        AccountMeta(buyer_ata, is_signer=False, is_writable=True),                 # 5
        AccountMeta(buyer, is_signer=True, is_writable=True),                      # 6
        AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),           # 7
        AccountMeta(token_program, is_signer=False, is_writable=False),            # 8
        AccountMeta(creator_vault, is_signer=False, is_writable=True),             # 9
        AccountMeta(EVENT_AUTHORITY, is_signer=False, is_writable=False),          # 10
        AccountMeta(PUMP_PROGRAM, is_signer=False, is_writable=False),             # 11
        AccountMeta(GLOBAL_VOLUME_ACCUMULATOR, is_signer=False, is_writable=False),# 12
        AccountMeta(user_vol, is_signer=False, is_writable=True),                  # 13
        AccountMeta(FEE_CONFIG, is_signer=False, is_writable=False),               # 14
        AccountMeta(FEE_PROGRAM, is_signer=False, is_writable=False),              # 15
        AccountMeta(bonding_curve_v2, is_signer=False, is_writable=False),         # 16 (NEW, last)
    ]
    return Instruction(PUMP_PROGRAM, data, accounts)

def build_sell_ix(
    mint: Pubkey, seller: Pubkey, creator: Pubkey,
    token_amount: int, min_sol_out: int, cashback_enabled: bool,
    token_program: Pubkey = TOKEN_PROGRAM,
    fee_recipient: Pubkey = FEE_RECIPIENT,
) -> Instruction:
    """sell — 15 accounts (non-cashback) or 16 (cashback: +user_volume_accumulator).
    bonding_curve_v2 is always the last account."""
    bonding_curve = get_bonding_curve_address(mint)
    bonding_curve_ata = get_ata(bonding_curve, mint, token_program)
    seller_ata = get_ata(seller, mint, token_program)
    creator_vault = get_creator_vault_address(creator)
    bonding_curve_v2 = get_bonding_curve_v2_address(mint)

    data = SELL_DISC + struct.pack("<QQ", token_amount, min_sol_out)
    accounts = [
        AccountMeta(GLOBAL_PDA, is_signer=False, is_writable=False),        # 0
        AccountMeta(fee_recipient, is_signer=False, is_writable=True),      # 1
        AccountMeta(mint, is_signer=False, is_writable=False),             # 2
        AccountMeta(bonding_curve, is_signer=False, is_writable=True),      # 3
        AccountMeta(bonding_curve_ata, is_signer=False, is_writable=True),  # 4
        AccountMeta(seller_ata, is_signer=False, is_writable=True),         # 5
        AccountMeta(seller, is_signer=True, is_writable=True),              # 6
        AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),    # 7
        AccountMeta(creator_vault, is_signer=False, is_writable=True),      # 8
        AccountMeta(token_program, is_signer=False, is_writable=False),     # 9
        AccountMeta(EVENT_AUTHORITY, is_signer=False, is_writable=False),   # 10
        AccountMeta(PUMP_PROGRAM, is_signer=False, is_writable=False),      # 11
        AccountMeta(FEE_CONFIG, is_signer=False, is_writable=False),        # 12
        AccountMeta(FEE_PROGRAM, is_signer=False, is_writable=False),       # 13
    ]
    # Cashback tokens need user_volume_accumulator BEFORE bonding_curve_v2
    if cashback_enabled:
        user_vol = get_user_volume_accumulator_address(seller)
        accounts.append(AccountMeta(user_vol, is_signer=False, is_writable=True))  # 14
    accounts.append(AccountMeta(bonding_curve_v2, is_signer=False, is_writable=False))  # last
    return Instruction(PUMP_PROGRAM, data, accounts)

def build_create_ata_idempotent_ix(payer: Pubkey, owner: Pubkey, mint: Pubkey,
                                   token_program: Pubkey = TOKEN_PROGRAM) -> Instruction:
    """Idempotent ATA creation (instruction discriminator 1 = create_idempotent).
    Won't fail if the ATA already exists."""
    ata = get_ata(owner, mint, token_program)
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM,
        bytes([1]),
        [
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(ata, is_signer=False, is_writable=True),
            AccountMeta(owner, is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(token_program, is_signer=False, is_writable=False),
        ],
    )

async def send_tx_rpc(keypair: Keypair, instructions: list) -> str:
    """Send transaction via normal RPC (used for sells — no Jito tip needed)."""
    buyer = keypair.pubkey()
    blockhash = Hash.from_string(await get_recent_blockhash())
    msg = MessageV0.try_compile(
        payer=buyer, instructions=instructions,
        address_lookup_table_accounts=[], recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [keypair])
    tx_base58 = base58.b58encode(bytes(tx)).decode()

    async with aiohttp.ClientSession() as session:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "sendTransaction",
            "params": [tx_base58, {"skipPreflight": True, "encoding": "base58"}],
        }
        async with session.post(RPC_URL, json=payload) as resp:
            result = await resp.json()
            if "error" in result:
                raise Exception(result["error"])
            return result.get("result", "sent")

async def send_tx_jito(keypair: Keypair, instructions: list, tip: float) -> str:
    buyer = keypair.pubkey()
    blockhash = Hash.from_string(await get_recent_blockhash())

    tip_account = Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS))
    tip_ix = transfer(TransferParams(
        from_pubkey=buyer, to_pubkey=tip_account, lamports=int(tip * 1e9),
    ))

    all_ixs = instructions + [tip_ix]
    msg = MessageV0.try_compile(
        payer=buyer, instructions=all_ixs,
        address_lookup_table_accounts=[], recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [keypair])
    tx_base58 = base58.b58encode(bytes(tx)).decode()

    # Each wallet sends its OWN separate bundle — never bundled together
    async with aiohttp.ClientSession() as session:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "sendBundle", "params": [[tx_base58]],
        }
        async with session.post(JITO_URL, json=payload) as resp:
            result = await resp.json()
            if "error" in result:
                raise Exception(result["error"])
            return result.get("result", "sent")

# ─── Bot Setup ───────────────────────────────────────────────────────────────

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
active_snipes: dict[int, asyncio.Task] = {}

# ─── UI Components ──────────────────────────────────────────────────────────

class WalletSelect(discord.ui.Select):
    def __init__(self, wallets_with_balances):
        options = []
        for i, (addr, bal) in enumerate(wallets_with_balances):
            short = addr[:6] + "..." + addr[-4:]
            options.append(discord.SelectOption(
                label=f"Wallet {i+1}: {short}",
                description=f"{bal:.4f} SOL",
                value=str(i),
            ))
        super().__init__(
            placeholder="Select active wallets...",
            min_values=1, max_values=len(options), options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        config = load_config()
        config["active_wallets"] = [int(v) for v in self.values]
        save_config(config)
        selected = ", ".join([f"Wallet {int(v)+1}" for v in self.values])
        await interaction.response.send_message(f"✅ Active wallets: {selected}", ephemeral=True)

class WalletView(discord.ui.View):
    def __init__(self, wallets_with_balances):
        super().__init__(timeout=60)
        self.add_item(WalletSelect(wallets_with_balances))

class SellSelect(discord.ui.Select):
    def __init__(self, mint: str, options_list):
        self.mint = mint
        super().__init__(placeholder="Select % to sell...", options=options_list)

    async def callback(self, interaction: discord.Interaction):
        pct = int(self.values[0])
        await interaction.response.defer(ephemeral=True)
        config = load_config()
        active = config.get("active_wallets", list(range(len(config["wallets"]))))
        mint_pubkey = Pubkey.from_string(self.mint)
        bonding_curve = get_bonding_curve_address(mint_pubkey)
        settings = config.get("settings", {})

        # Read creator + cashback flag + token program once
        curve_state = await get_bonding_curve_state(str(bonding_curve))
        creator = curve_state["creator"] if curve_state and curve_state.get("creator") else mint_pubkey
        cashback = curve_state.get("cashback_enabled", False) if curve_state else False
        token_program = await get_mint_owner(self.mint)

        results = []
        # Stagger sells with random delays to avoid linking wallets
        for i in active:
            try:
                kp = Keypair.from_base58_string(config["wallets"][i])
                seller = kp.pubkey()
                balance = await get_token_balance(str(seller), self.mint)
                if balance <= 0:
                    results.append(f"❌ Wallet {i+1}: no tokens")
                    continue
                sell_amount = int(balance * (pct / 100) * 1e6)
                sell_ix = build_sell_ix(
                    mint_pubkey, seller, creator, sell_amount, 0, cashback, token_program,
                )
                cu_limit = set_compute_unit_limit(250_000)
                cu_price = set_compute_unit_price(settings.get("priority_fee", 200000))
                sig = await send_tx_rpc(kp, [cu_limit, cu_price, sell_ix])
                results.append(f"✅ Wallet {i+1}: sold {pct}% — `{sig}`")
                # Random delay between sells to avoid on-chain linking
                await asyncio.sleep(random.uniform(1.5, 5.0))
            except Exception as e:
                results.append(f"❌ Wallet {i+1}: {e}")

        await interaction.followup.send("\n".join(results), ephemeral=True)

class SellView(discord.ui.View):
    def __init__(self, mint: str):
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(label="25%", value="25"),
            discord.SelectOption(label="50%", value="50"),
            discord.SelectOption(label="75%", value="75"),
            discord.SelectOption(label="100%", value="100"),
        ]
        self.add_item(SellSelect(mint, options))

# ─── Commands ────────────────────────────────────────────────────────────────

@tree.command(name="cmds", description="Show all commands")
async def cmds(interaction: discord.Interaction):
    embed = discord.Embed(title="🔫 Pump.fun Sniper Bot", color=0x00FF00)
    embed.add_field(name="__Wallet Management__", value=(
        "`/addwallet` — Add a wallet private key\n"
        "`/removewallet` — Remove wallet by index\n"
        "`/manage` — View balances & select active wallets\n"
        "`/balances` — Quick balance check all wallets"
    ), inline=False)
    embed.add_field(name="__Sniping__", value=(
        "`/snipe` — Watch dev wallet & auto-buy on launch\n"
        "`/cancel` — Cancel active snipe\n"
        "`/status` — Check if a snipe is active"
    ), inline=False)
    embed.add_field(name="__Trading__", value=(
        "`/sell` — Sell % of a token (dropdown selector)\n"
        "`/sellall` — Dump 100% of a token from all wallets\n"
        "`/holdings` — Check token holdings across wallets"
    ), inline=False)
    embed.add_field(name="__Settings__", value=(
        "`/settings` — View current settings\n"
        "`/setslippage` — Set slippage %\n"
        "`/settip` — Set Jito tip amount\n"
        "`/setpriority` — Set priority fee"
    ), inline=False)
    embed.add_field(name="__Safety & Info__", value=(
        "`/antilink` — Verify wallets have no on-chain links\n"
        "`/pnl` — View snipe history\n"
        "`/solprice` — Current SOL price"
    ), inline=False)
    embed.set_footer(text="Wallets never transact with each other • Sells are staggered randomly")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── Wallet Management ──

@tree.command(name="addwallet", description="Add a wallet private key")
@app_commands.describe(private_key="Base58 private key")
async def addwallet(interaction: discord.Interaction, private_key: str):
    try:
        kp = Keypair.from_base58_string(private_key)
    except:
        await interaction.response.send_message("❌ Invalid private key.", ephemeral=True)
        return

    config = load_config()
    new_pubkey = str(kp.pubkey())

    existing = get_wallet_pubkeys(config)
    if new_pubkey in existing:
        await interaction.response.send_message("❌ Wallet already added.", ephemeral=True)
        return

    if len(config["wallets"]) >= 10:
        await interaction.response.send_message("❌ Max 10 wallets.", ephemeral=True)
        return

    config["wallets"].append(private_key)
    save_config(config)
    short = new_pubkey[:6] + "..." + new_pubkey[-4:]
    await interaction.response.send_message(
        f"✅ Added wallet {len(config['wallets'])}: `{short}`", ephemeral=True
    )

@tree.command(name="removewallet", description="Remove a wallet by index")
@app_commands.describe(index="Wallet number (1-based)")
async def removewallet(interaction: discord.Interaction, index: int):
    config = load_config()
    idx = index - 1
    if idx < 0 or idx >= len(config["wallets"]):
        await interaction.response.send_message("❌ Invalid index.", ephemeral=True)
        return
    config["wallets"].pop(idx)
    config["active_wallets"] = [i for i in config["active_wallets"] if i < len(config["wallets"])]
    save_config(config)
    await interaction.response.send_message(f"✅ Removed wallet {index}.", ephemeral=True)

@tree.command(name="manage", description="View wallet balances & select active wallets")
async def manage(interaction: discord.Interaction):
    config = load_config()
    if not config["wallets"]:
        await interaction.response.send_message("No wallets. Use `/addwallet` first.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    wallets_with_balances = []
    for pk in config["wallets"]:
        kp = Keypair.from_base58_string(pk)
        pubkey = str(kp.pubkey())
        try:
            bal = await get_balance(pubkey)
        except:
            bal = 0.0
        wallets_with_balances.append((pubkey, bal))

    total = sum(b for _, b in wallets_with_balances)
    embed = discord.Embed(title="🔫 Sniper Wallets", color=0x00FF00)
    for i, (addr, bal) in enumerate(wallets_with_balances):
        short = addr[:6] + "..." + addr[-4:]
        active = "✅" if i in config.get("active_wallets", []) else "⬜"
        embed.add_field(
            name=f"{active} Wallet {i+1}",
            value=f"`{short}`\n{bal:.4f} SOL",
            inline=True,
        )
    embed.set_footer(text=f"Total: {total:.4f} SOL • Select wallets below")

    view = WalletView(wallets_with_balances)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@tree.command(name="balances", description="Quick balance check")
async def balances(interaction: discord.Interaction):
    config = load_config()
    if not config["wallets"]:
        await interaction.response.send_message("No wallets.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    lines = []
    total = 0.0
    for i, pk in enumerate(config["wallets"]):
        kp = Keypair.from_base58_string(pk)
        pubkey = str(kp.pubkey())
        try:
            bal = await get_balance(pubkey)
        except:
            bal = 0.0
        total += bal
        short = pubkey[:6] + "..." + pubkey[-4:]
        active = "✅" if i in config.get("active_wallets", []) else "⬜"
        lines.append(f"{active} **Wallet {i+1}** `{short}` — {bal:.4f} SOL")

    sol_price = await get_sol_price()
    usd_total = total * sol_price if sol_price else 0

    embed = discord.Embed(title="💰 Wallet Balances", color=0x00FF00)
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Total: {total:.4f} SOL (${usd_total:.2f})")
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── Anti-Link ──

@tree.command(name="antilink", description="Verify wallets have no on-chain links")
async def antilink(interaction: discord.Interaction):
    config = load_config()
    if len(config["wallets"]) < 2:
        await interaction.response.send_message("Need 2+ wallets to check.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    pubkeys = get_wallet_pubkeys(config)
    issues = []

    async with aiohttp.ClientSession() as session:
        for i, pk_a in enumerate(pubkeys):
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [pk_a, {"limit": 50}],
            }
            try:
                async with session.post(RPC_URL, json=payload) as resp:
                    data = await resp.json()
                    sigs = data.get("result", [])

                for sig_info in sigs[:20]:  # check last 20 txs per wallet
                    tx_payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTransaction",
                        "params": [sig_info["signature"], {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                        }],
                    }
                    async with session.post(RPC_URL, json=tx_payload) as resp:
                        tx_data = await resp.json()
                        tx = tx_data.get("result")

                    if not tx:
                        continue

                    msg = tx.get("transaction", {}).get("message", {})
                    account_keys = []
                    for ak in msg.get("accountKeys", []):
                        if isinstance(ak, dict):
                            account_keys.append(ak.get("pubkey", ""))
                        else:
                            account_keys.append(ak)

                    for j, pk_b in enumerate(pubkeys):
                        if j != i and pk_b in account_keys:
                            issues.append(
                                f"⚠️ Wallet {i+1} ↔ Wallet {j+1} in tx `{sig_info['signature'][:16]}...`"
                            )
            except:
                issues.append(f"⚠️ Could not check Wallet {i+1}")

    # Deduplicate (A↔B and B↔A)
    seen = set()
    unique_issues = []
    for issue in issues:
        key = frozenset(issue.split("↔")[:2])
        if key not in seen:
            seen.add(key)
            unique_issues.append(issue)

    if unique_issues:
        embed = discord.Embed(title="🔴 Anti-Link Issues", color=0xFF0000)
        embed.description = "\n".join(unique_issues[:20])
        embed.set_footer(text="These wallets may be linkable on-chain. Use fresh wallets.")
    else:
        embed = discord.Embed(title="🟢 Anti-Link Clear", color=0x00FF00)
        embed.description = "No cross-wallet transactions found. Wallets look independent."

    await interaction.followup.send(embed=embed, ephemeral=True)

# ── Settings ──

@tree.command(name="settings", description="View current settings")
async def settings_cmd(interaction: discord.Interaction):
    config = load_config()
    s = config.get("settings", {})
    embed = discord.Embed(title="⚙️ Settings", color=0x5865F2)
    embed.add_field(name="Slippage", value=f"{s.get('slippage', 50)}%", inline=True)
    embed.add_field(name="Jito Tip", value=f"{s.get('jito_tip', 0.001)} SOL", inline=True)
    embed.add_field(name="Priority Fee", value=f"{s.get('priority_fee', 200000)} μlamports", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="setslippage", description="Set slippage percentage")
@app_commands.describe(percent="Slippage % (e.g. 50)")
async def setslippage(interaction: discord.Interaction, percent: int):
    config = load_config()
    config.setdefault("settings", {})["slippage"] = max(1, min(percent, 100))
    save_config(config)
    await interaction.response.send_message(f"✅ Slippage: {percent}%", ephemeral=True)

@tree.command(name="settip", description="Set Jito tip amount in SOL")
@app_commands.describe(amount="Tip in SOL (e.g. 0.001)")
async def settip(interaction: discord.Interaction, amount: float):
    config = load_config()
    config.setdefault("settings", {})["jito_tip"] = max(0.0001, amount)
    save_config(config)
    await interaction.response.send_message(f"✅ Jito tip: {amount} SOL", ephemeral=True)

@tree.command(name="setpriority", description="Set priority fee in microlamports")
@app_commands.describe(fee="Priority fee (e.g. 200000)")
async def setpriority(interaction: discord.Interaction, fee: int):
    config = load_config()
    config.setdefault("settings", {})["priority_fee"] = max(1000, fee)
    save_config(config)
    await interaction.response.send_message(f"✅ Priority fee: {fee}", ephemeral=True)

# ── Sniping ──

@tree.command(name="snipe", description="Snipe a dev wallet's next token launch")
@app_commands.describe(
    wallet="Dev wallet address to watch",
    amount="Total SOL to spend (split across active wallets)",
)
async def snipe(interaction: discord.Interaction, wallet: str, amount: float):
    config = load_config()
    active = config.get("active_wallets", [])
    if not active:
        await interaction.response.send_message(
            "❌ No active wallets. Use `/manage` first.", ephemeral=True
        )
        return

    if interaction.user.id in active_snipes:
        await interaction.response.send_message(
            "❌ Already sniping. `/cancel` first.", ephemeral=True
        )
        return

    # Anti-link: verify no duplicate pubkeys in active set
    pubkeys = get_wallet_pubkeys(config)
    active_pubkeys = [pubkeys[i] for i in active]
    if len(set(active_pubkeys)) != len(active_pubkeys):
        await interaction.response.send_message("❌ Duplicate wallet detected.", ephemeral=True)
        return

    per_wallet = amount / len(active)

    embed = discord.Embed(title="🎯 Snipe Armed", color=0xFF5500)
    embed.add_field(name="Dev Wallet", value=f"`{wallet}`", inline=False)
    embed.add_field(name="Total", value=f"{amount} SOL", inline=True)
    embed.add_field(name="Per Wallet", value=f"{per_wallet:.4f} SOL", inline=True)
    embed.add_field(name="Wallets", value=", ".join([f"W{i+1}" for i in active]), inline=False)
    embed.set_footer(text="Watching... /cancel to stop")

    await interaction.response.send_message(embed=embed, ephemeral=True)

    task = asyncio.create_task(
        watch_and_snipe(interaction, wallet, amount, active, config)
    )
    active_snipes[interaction.user.id] = task

@tree.command(name="cancel", description="Cancel active snipe")
async def cancel(interaction: discord.Interaction):
    task = active_snipes.pop(interaction.user.id, None)
    if task:
        task.cancel()
        await interaction.response.send_message("✅ Snipe cancelled.", ephemeral=True)
    else:
        await interaction.response.send_message("No active snipe.", ephemeral=True)

@tree.command(name="status", description="Check snipe status")
async def status(interaction: discord.Interaction):
    if interaction.user.id in active_snipes:
        await interaction.response.send_message("🎯 Snipe **active**.", ephemeral=True)
    else:
        await interaction.response.send_message("No active snipe.", ephemeral=True)

# ── Trading ──

@tree.command(name="sell", description="Sell a token with % selector")
@app_commands.describe(mint="Token mint address")
async def sell(interaction: discord.Interaction, mint: str):
    view = SellView(mint)
    await interaction.response.send_message(
        f"Select % to sell for `{mint[:8]}...`:", view=view, ephemeral=True
    )

@tree.command(name="sellall", description="Dump 100% of a token from all active wallets")
@app_commands.describe(mint="Token mint address")
async def sellall(interaction: discord.Interaction, mint: str):
    await interaction.response.defer(ephemeral=True)
    config = load_config()
    active = config.get("active_wallets", list(range(len(config["wallets"]))))
    settings = config.get("settings", {})
    mint_pubkey = Pubkey.from_string(mint)
    bonding_curve = get_bonding_curve_address(mint_pubkey)

    curve_state = await get_bonding_curve_state(str(bonding_curve))
    creator = curve_state["creator"] if curve_state and curve_state.get("creator") else mint_pubkey
    cashback = curve_state.get("cashback_enabled", False) if curve_state else False
    token_program = await get_mint_owner(mint)

    results = []
    for i in active:
        try:
            kp = Keypair.from_base58_string(config["wallets"][i])
            seller = kp.pubkey()
            balance = await get_token_balance(str(seller), mint)
            if balance <= 0:
                results.append(f"⬜ Wallet {i+1}: no tokens")
                continue
            sell_amount = int(balance * 1e6)
            sell_ix = build_sell_ix(
                mint_pubkey, seller, creator, sell_amount, 0, cashback, token_program,
            )
            cu_limit = set_compute_unit_limit(250_000)
            cu_price = set_compute_unit_price(settings.get("priority_fee", 200000))
            sig = await send_tx_rpc(kp, [cu_limit, cu_price, sell_ix])
            results.append(f"✅ Wallet {i+1}: sold all — `{sig}`")
            await asyncio.sleep(random.uniform(1.5, 5.0))
        except Exception as e:
            results.append(f"❌ Wallet {i+1}: {e}")

    await interaction.followup.send("\n".join(results), ephemeral=True)

@tree.command(name="holdings", description="Check token holdings across wallets")
@app_commands.describe(mint="Token mint address")
async def holdings(interaction: discord.Interaction, mint: str):
    await interaction.response.defer(ephemeral=True)
    config = load_config()
    embed = discord.Embed(title=f"📊 Holdings — `{mint[:8]}...`", color=0x5865F2)
    total = 0.0
    for i, pk in enumerate(config["wallets"]):
        kp = Keypair.from_base58_string(pk)
        pubkey = str(kp.pubkey())
        short = pubkey[:6] + "..." + pubkey[-4:]
        try:
            bal = await get_token_balance(pubkey, mint)
        except:
            bal = 0.0
        total += bal
        active = "✅" if i in config.get("active_wallets", []) else "⬜"
        embed.add_field(name=f"{active} Wallet {i+1}", value=f"`{short}`\n{bal:,.2f} tokens", inline=True)

    embed.set_footer(text=f"Total: {total:,.2f} tokens")
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── Info ──

@tree.command(name="solprice", description="Current SOL price")
async def solprice(interaction: discord.Interaction):
    price = await get_sol_price()
    if price:
        await interaction.response.send_message(f"◎ SOL = **${price:,.2f}**", ephemeral=True)
    else:
        await interaction.response.send_message("Couldn't fetch price.", ephemeral=True)

@tree.command(name="pnl", description="View snipe history")
async def pnl(interaction: discord.Interaction):
    config = load_config()
    history = config.get("snipe_history", [])
    if not history:
        await interaction.response.send_message("No snipe history yet.", ephemeral=True)
        return

    embed = discord.Embed(title="📈 Snipe History", color=0x00FF00)
    for entry in history[-10:]:
        embed.add_field(
            name=f"`{entry['mint'][:8]}...`",
            value=(
                f"Spent: {entry['total_sol']:.4f} SOL\n"
                f"Wallets: {entry['wallet_count']}\n"
                f"Time: {entry['timestamp'][:16]}"
            ),
            inline=True,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─── Snipe Watcher ───────────────────────────────────────────────────────────

async def watch_and_snipe(
    interaction: discord.Interaction,
    dev_wallet: str,
    total_sol: float,
    active_indices: list,
    config: dict,
):
    channel = interaction.channel
    settings = config.get("settings", {})
    slippage_pct = settings.get("slippage", 50)
    tip = settings.get("jito_tip", 0.001)
    priority = settings.get("priority_fee", 200000)
    seen_sigs = set()

    # Anchor: grab current sigs so we don't trigger on old txs
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [dev_wallet, {"limit": 10, "commitment": "confirmed"}],
            }
            async with session.post(RPC_URL, json=payload) as resp:
                data = await resp.json()
                for s in data.get("result", []):
                    seen_sigs.add(s["signature"])
    except:
        pass

    await channel.send(f"👀 Watching `{dev_wallet[:8]}...` for Pump.fun launch...")

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [dev_wallet, {"limit": 5, "commitment": "confirmed"}],
                }
                async with session.post(RPC_URL, json=payload) as resp:
                    data = await resp.json()
                    sigs = data.get("result", [])

                if not sigs:
                    await asyncio.sleep(0.5)
                    continue

                # Check newest sigs we haven't seen
                new_sigs = [s for s in sigs if s["signature"] not in seen_sigs]
                for sig_info in new_sigs:
                    sig = sig_info["signature"]
                    seen_sigs.add(sig)

                    tx_payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTransaction",
                        "params": [sig, {
                            "encoding": "jsonParsed",
                            "commitment": "confirmed",
                            "maxSupportedTransactionVersion": 0,
                        }],
                    }
                    async with session.post(RPC_URL, json=tx_payload) as resp:
                        tx_data = await resp.json()
                        tx = tx_data.get("result")

                    if not tx:
                        continue

                    # Check for failed tx
                    if tx.get("meta", {}).get("err"):
                        continue

                    msg = tx.get("transaction", {}).get("message", {})
                    account_keys = []
                    for ak in msg.get("accountKeys", []):
                        if isinstance(ak, dict):
                            account_keys.append(ak.get("pubkey", ""))
                        else:
                            account_keys.append(ak)

                    if str(PUMP_PROGRAM) not in account_keys:
                        continue

                    # Look for a new token mint in the transaction
                    # Check log messages for "Create" to confirm it's a token creation
                    log_msgs = tx.get("meta", {}).get("logMessages", [])
                    is_create = any("Create" in log or "InitializeMint" in log for log in log_msgs)
                    if not is_create:
                        # Could be a buy/sell by the dev, not a new launch
                        continue

                    post_balances = tx.get("meta", {}).get("postTokenBalances", [])
                    mint_address = None
                    for tb in post_balances:
                        m = tb.get("mint", "")
                        if m and m != str(TOKEN_PROGRAM):
                            mint_address = m
                            break

                    if not mint_address:
                        continue

                    await channel.send(
                        f"@everyone 🚀 **Token launched!** `{mint_address}`\n"
                        f"Firing {len(active_indices)} buys..."
                    )

                    mint_pubkey = Pubkey.from_string(mint_address)
                    bonding_curve = get_bonding_curve_address(mint_pubkey)
                    per_wallet = total_sol / len(active_indices)
                    sol_lamports = int(per_wallet * 1e9)

                    # Resolve creator + token program from chain. The create tx we
                    # detected has account[6] = creator; but reading the bonding
                    # curve is authoritative and also gives reserves for min-out.
                    creator = None
                    token_program = TOKEN_PROGRAM
                    min_tokens_out = 0
                    try:
                        curve_state = await get_bonding_curve_state(str(bonding_curve))
                        if curve_state and curve_state.get("creator"):
                            creator = curve_state["creator"]
                            expected = calculate_tokens_out(sol_lamports, curve_state)
                            # floor tokens at (1 - slippage); 0 = accept any (aggressive)
                            min_tokens_out = int(expected * (1 - slippage_pct / 100))
                    except Exception as e:
                        print(f"curve read failed: {e}")

                    # Fall back to the create tx's creator (account index 6)
                    if creator is None:
                        try:
                            if len(account_keys) > 6:
                                creator = Pubkey.from_string(account_keys[6])
                        except:
                            pass
                    if creator is None:
                        creator = mint_pubkey  # last-ditch; will likely fail, logged below

                    try:
                        token_program = await get_mint_owner(mint_address)
                    except:
                        token_program = TOKEN_PROGRAM

                    async def buy_for_wallet(wallet_idx):
                        """Each wallet builds and sends its own independent tx (no cross-wallet linking)."""
                        kp = Keypair.from_base58_string(config["wallets"][wallet_idx])
                        buyer = kp.pubkey()

                        create_ata_ix = build_create_ata_idempotent_ix(
                            buyer, buyer, mint_pubkey, token_program
                        )
                        buy_ix = build_buy_ix(
                            mint_pubkey, buyer, creator,
                            sol_lamports, min_tokens_out, token_program,
                        )
                        cu_limit = set_compute_unit_limit(250_000)
                        cu_price = set_compute_unit_price(priority)
                        return await send_tx_rpc(kp, [cu_limit, cu_price, create_ata_ix, buy_ix])

                    # Fire ALL buys concurrently — speed matters for sniping
                    results = await asyncio.gather(
                        *[buy_for_wallet(i) for i in active_indices],
                        return_exceptions=True,
                    )

                    for i, result in zip(active_indices, results):
                        if isinstance(result, Exception):
                            await channel.send(f"❌ Wallet {i+1}: {result}")
                        else:
                            await channel.send(f"✅ Wallet {i+1}: `{result}`")

                    # Log history
                    config = load_config()
                    config.setdefault("snipe_history", []).append({
                        "mint": mint_address,
                        "total_sol": total_sol,
                        "wallet_count": len(active_indices),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    save_config(config)
                    active_snipes.pop(interaction.user.id, None)
                    return

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"Watch error: {e}")

        await asyncio.sleep(0.5)

# ─── Startup ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await tree.sync()
    print(f"🔫 Sniper bot online as {bot.user}")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Set DISCORD_TOKEN environment variable")
        exit(1)
    bot.run(token)
