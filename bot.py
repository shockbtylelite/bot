import discord
from discord.ext import commands
import json, os, asyncio, re

BOT_TOKEN       = "MTQ4MDE2NTkzMDEwMDE5OTQyNA.GNW0fH.x1lDKZDnRDg6lolzMbkJnERv4rATjgN_PzPDPI"
ADMIN_ROLE_ID   = 1478445114425606388
SUPPORT_ROLE_ID = 1480193319341396128
DATA_FILE       = "vps_data.json"
VPS_RAM_MB      = 512          # memory limit per container
VPS_CPUS        = 0.25         # docker --cpus (float)
VPS_DISK_GB     = 100          # informational only (shown to user)

# ── Docker Hub images per OS ──────────────────────────────────────────────
OS_OPTIONS = {
    "1":  ("Ubuntu 24.04 (Noble)",   "ubuntu:noble"),
    "2":  ("Ubuntu 22.04 (Jammy)",   "ubuntu:jammy"),
    "3":  ("Ubuntu 20.04 (Focal)",   "ubuntu:focal"),
    "4":  ("Debian 13 (Trixie)",     "debian:trixie"),
    "5":  ("Debian 12 (Bookworm)",   "debian:bookworm"),
    "6":  ("Debian 11 (Bullseye)",   "debian:bullseye"),
    "7":  ("Kali Linux (Rolling)",   "kalilinux/kali-rolling"),
    "8":  ("Alpine Linux 3.19",      "alpine:3.19"),
    "9":  ("Fedora 39",              "fedora:39"),
    "10": ("CentOS Stream 9",        "quay.io/centos/centos:stream9"),
    "11": ("Arch Linux",             "archlinux:latest"),
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

active_talks:  dict = {}   # uid <-> uid
support_queue: dict = {}
all_messages:  dict = {}   # channel_id -> [(kind, msg_id)]

REAL_CMDS = {
    "createvps","createsomeonevps","sshx","tmate","start","stop",
    "deletevps","userdeletevps","status","vpslist","listvps",
    "commands","clear","viewothervps","talk","talk2","endtalk","say","support",
}

# ═══════════════════════════════ HELPERS ══════════════════════════════════

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_vps(data, user_key, index):
    for v in data.get(user_key, {}).get("vps_list", []):
        if v["index"] == index:
            return v
    return None

def next_index(data, user_key):
    lst = data.get(user_key, {}).get("vps_list", [])
    return max((v["index"] for v in lst), default=0) + 1

async def run(cmd):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

# ── Docker helpers (replace all lxc-* calls) ─────────────────────────────

async def docker_exec(ct, bash_cmd):
    """Run a bash command inside a running Docker container."""
    return await run(["docker", "exec", ct, "bash", "-c", bash_cmd])

async def docker_sh(ct, sh_cmd):
    """Run a sh command (Alpine-safe fallback)."""
    return await run(["docker", "exec", ct, "sh", "-c", sh_cmd])

async def docker_pkg(ct, pkg):
    """Install a package — works on apt, apk, dnf, and pacman distros."""
    install_cmd = (
        f"if command -v apt-get >/dev/null 2>&1; then "
        f"  apt-get update -qq 2>/dev/null && apt-get install -y {pkg} -qq 2>/dev/null; "
        f"elif command -v apk >/dev/null 2>&1; then "
        f"  apk add --no-cache {pkg} 2>/dev/null; "
        f"elif command -v dnf >/dev/null 2>&1; then "
        f"  dnf install -y {pkg} 2>/dev/null; "
        f"elif command -v pacman >/dev/null 2>&1; then "
        f"  pacman -Sy --noconfirm {pkg} 2>/dev/null; "
        f"fi"
    )
    return await docker_sh(ct, install_cmd)

async def docker_status(ct):
    """Return True if container is running."""
    rc, out, _ = await run(
        ["docker", "inspect", "-f", "{{.State.Status}}", ct])
    return rc == 0 and out.strip() == "running"

async def docker_ip(ct):
    """Get container's internal IP."""
    _, out, _ = await run([
        "docker", "inspect", "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", ct
    ])
    return out.strip() or "unavailable"

async def ensure_curl(ct):
    rc, _, _ = await docker_sh(ct, "command -v curl")
    if rc != 0:
        await docker_pkg(ct, "curl")

# ── Permission helpers ────────────────────────────────────────────────────

def is_admin(member):
    return any(r.id == ADMIN_ROLE_ID for r in member.roles)

def has_admin_role():
    async def predicate(ctx):
        if not ctx.guild:
            await ctx.send("❌ Use this command in the server.")
            return False
        if is_admin(ctx.author):
            return True
        await ctx.send("❌ You don't have permission.")
        return False
    return commands.check(predicate)

# ── Message helpers ───────────────────────────────────────────────────────

async def bot_send(channel, *args, **kwargs):
    msg = await channel.send(*args, **kwargs)
    all_messages.setdefault(channel.id, []).append(("bot", msg.id))
    return msg

async def wait_channel(ctx, delete_reply=False):
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel
    try:
        reply = await bot.wait_for("message", check=check, timeout=300)
        all_messages.setdefault(ctx.channel.id, []).append(("user", reply.id))
        if delete_reply:
            try: await reply.delete()
            except Exception: pass
        return reply
    except asyncio.TimeoutError:
        return None

async def wait_dm(user, delete_reply=False):
    def check(m):
        return m.author.id == user.id and isinstance(m.channel, discord.DMChannel)
    try:
        reply = await bot.wait_for("message", check=check, timeout=600)
        if delete_reply:
            try: await reply.delete()
            except Exception: pass
        return reply
    except asyncio.TimeoutError:
        return None

# ═══════════════════════════ SETUP WIZARDS ════════════════════════════════

async def guild_wizard(ctx):
    os_list = "\n".join(f"**{k}.** {v[0]}" for k, v in OS_OPTIONS.items())
    s1 = await bot_send(ctx.channel, f"🖥️ **Step 1 — Choose OS**\n{os_list}\n\nType the number:")
    r1 = await wait_channel(ctx)
    if not r1 or r1.content.strip() not in OS_OPTIONS:
        await s1.edit(content="❌ Invalid. Run `!createvps` again."); return None
    os_name, image = OS_OPTIONS[r1.content.strip()]
    await s1.edit(content=f"✅ **Step 1 — OS:** {os_name}")

    s2 = await bot_send(ctx.channel, "📛 **Step 2 — Container Name**\nType a name (e.g. `myserver`):")
    r2 = await wait_channel(ctx)
    if not r2: await s2.edit(content="❌ No reply."); return None
    ct_name = re.sub(r"[^a-z0-9\-]", "-", r2.content.strip().lower())[:32] or "myserver"
    await s2.edit(content=f"✅ **Step 2 — Name:** `{ct_name}`")

    s3 = await bot_send(ctx.channel, "👤 **Step 3 — Username**\nType your username (or `root`):")
    r3 = await wait_channel(ctx)
    if not r3: await s3.edit(content="❌ No reply."); return None
    username = r3.content.strip().lower()
    await s3.edit(content=f"✅ **Step 3 — Username:** `{username}`")

    s4 = await bot_send(ctx.channel, "🔑 **Step 4 — Password**\n*(Your message will be deleted instantly)*")
    r4 = await wait_channel(ctx, delete_reply=True)
    if not r4: await s4.edit(content="❌ No reply."); return None
    password = r4.content.strip()
    await s4.edit(content="✅ **Step 4 — Password:** `••••••••`")

    return os_name, image, ct_name, username, password

async def dm_wizard(user, admin_name):
    dm = user.dm_channel or await user.create_dm()
    os_list = "\n".join(f"**{k}.** {v[0]}" for k, v in OS_OPTIONS.items())
    await dm.send(
        f"👋 Hello **{user.display_name}**!\n"
        f"Admin **{admin_name}** is setting up a VPS for you.\n"
        f"Take your time — no rush!\n\n"
        f"🖥️ **Step 1 — Choose OS:**\n{os_list}\n\nType the number:"
    )
    r1 = await wait_dm(user)
    if not r1 or r1.content.strip() not in OS_OPTIONS:
        await dm.send("❌ Invalid OS. Ask admin to run `!createsomeonevps` again."); return None
    os_name, image = OS_OPTIONS[r1.content.strip()]
    await dm.send(f"✅ **OS:** {os_name}")

    await dm.send("📛 **Step 2 — Container Name**\nType a name (e.g. `myserver`):")
    r2 = await wait_dm(user)
    if not r2: await dm.send("❌ No reply."); return None
    ct_name = re.sub(r"[^a-z0-9\-]", "-", r2.content.strip().lower())[:32] or "myserver"
    await dm.send(f"✅ **Name:** `{ct_name}`")

    await dm.send("👤 **Step 3 — Username**\nType your username (or `root`):")
    r3 = await wait_dm(user)
    if not r3: await dm.send("❌ No reply."); return None
    username = r3.content.strip().lower()
    await dm.send(f"✅ **Username:** `{username}`")

    await dm.send("🔑 **Step 4 — Password**\n*(I'll delete it instantly for privacy)*")
    r4 = await wait_dm(user, delete_reply=True)
    if not r4: await dm.send("❌ No reply."); return None
    password = r4.content.strip()
    await dm.send("✅ **Password set!**\n\n⏳ Creating your VPS now, please wait…")

    return os_name, image, ct_name, username, password

# ═══════════════════════════ VPS CREATION ═════════════════════════════════

async def create_vps_core(owner, requester, setup, progress_msg=None):
    os_name, image, ct_custom, username, password = setup
    data = load_data()
    user_key = str(owner.id)
    if user_key not in data:
        data[user_key] = {"vps_list": []}

    index   = next_index(data, user_key)
    ct_name = ct_custom
    existing = [v["container_name"] for v in data[user_key]["vps_list"]]
    if ct_name in existing:
        ct_name = f"{ct_custom}-{index}"

    async def upd(txt):
        if progress_msg:
            await progress_msg.edit(content=txt)

    # ── Step 1: Pull image ──────────────────────────────────────────────
    await upd(f"⏳ Pulling Docker image `{image}`… (may take a minute)")
    rc, _, err = await run(["docker", "pull", image])
    if rc != 0:
        await upd(f"❌ `docker pull` failed:\n```{err[:600]}```"); return

    # ── Step 2: Run container ───────────────────────────────────────────
    await upd(f"⏳ Starting container `{ct_name}`…")
    rc, _, err = await run([
        "docker", "run", "-d",
        "--name",    ct_name,
        f"--memory={VPS_RAM_MB}m",
        "--memory-swap=0",          # no swap
        f"--cpus={VPS_CPUS}",
        "--cap-add=NET_ADMIN",
        "--cap-add=SYS_ADMIN",
        "--restart=unless-stopped",
        image,
        "tail", "-f", "/dev/null",  # keeps container alive
    ])
    if rc != 0:
        await upd(f"❌ `docker run` failed:\n```{err[:600]}```"); return

    await asyncio.sleep(3)

    # ── Step 3: Set up user ─────────────────────────────────────────────
    await upd(f"⏳ Setting up user `{username}`…")
    if username == "root":
        await docker_sh(ct_name,
            f"echo 'root:{password}' | chpasswd 2>/dev/null || "
            f"echo 'root:{password}' | chpasswd -e 2>/dev/null || true"
        )
    else:
        await docker_sh(ct_name,
            f"useradd -m -s /bin/bash {username} 2>/dev/null || true && "
            f"echo '{username}:{password}' | chpasswd 2>/dev/null || true && "
            f"usermod -aG sudo {username} 2>/dev/null || true && "
            f"usermod -aG wheel {username} 2>/dev/null || true"
        )

    # ── Step 4: Install & start SSH ─────────────────────────────────────
    await upd("⏳ Installing OpenSSH…")
    await docker_pkg(ct_name, "openssh-server")
    await docker_sh(ct_name,
        "mkdir -p /run/sshd /var/run/sshd && "
        "sed -i 's/.*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null || true && "
        "sed -i 's/.*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || true && "
        "ssh-keygen -A 2>/dev/null || true && "
        "( /usr/sbin/sshd -D & )"
    )

    await asyncio.sleep(3)
    ct_ip = await docker_ip(ct_name)

    # ── Save record ─────────────────────────────────────────────────────
    data[user_key]["vps_list"].append({
        "index": index, "container_name": ct_name, "os": os_name,
        "image": image, "username": username, "status": "running",
        "created_by": str(requester.id), "ip": ct_ip,
    })
    save_data(data)

    # ── Notify owner ─────────────────────────────────────────────────────
    dm_owner = owner.dm_channel or await owner.create_dm()
    if owner.id != requester.id:
        await dm_owner.send(
            f"👋 Hello **{owner.display_name}**!\n"
            f"Admin **{requester.display_name}** gave you **{ct_name}** 🎉\n\n"
            f"🖥️ OS: {os_name}\n🌐 IP: `{ct_ip}`\n"
            f"👤 Username: `{username}`\n🔑 Password: `{password}`\n\n"
            f"Use commands **in DMs with me**. Type `!commands` to see the list!"
        )
    else:
        await dm_owner.send(
            f"✅ **VPS Ready — `{ct_name}`**\n"
            f"🖥️ OS: {os_name}\n🌐 IP: `{ct_ip}`\n"
            f"👤 Username: `{username}`\n🔑 Password: `{password}`\n\n"
            f"Use `!sshx` or `!tmate` to connect."
        )

    await upd(
        f"✅ **VPS `{ct_name}` created!** {os_name}\n"
        f"💾 {VPS_DISK_GB}GB  •  🧠 {VPS_RAM_MB}MB  •  ⚙️ {VPS_CPUS} vCPU\n"
        f"📬 Credentials sent to owner's DMs."
    )

# ═══════════════════════════ GUILD COMMANDS ═══════════════════════════════

@bot.command(name="createvps")
@has_admin_role()
async def cmd_createvps(ctx):
    setup = await guild_wizard(ctx)
    if not setup: return
    prog = await bot_send(ctx.channel, "⏳ Starting VPS creation…")
    await create_vps_core(ctx.author, ctx.author, setup, prog)

@bot.command(name="createsomeonevps")
@has_admin_role()
async def cmd_createsomeonevps(ctx, member: discord.Member):
    prog = await bot_send(ctx.channel, f"⏳ Sending setup wizard to **{member.display_name}** in DMs…")
    try:
        setup = await dm_wizard(member, ctx.author.display_name)
    except discord.Forbidden:
        await prog.edit(content=f"❌ Can't DM {member.mention}. They need to allow DMs."); return
    if not setup:
        await prog.edit(content=f"❌ {member.display_name} didn't complete setup."); return
    await prog.edit(content=f"⏳ Creating VPS for **{member.display_name}**…")
    await create_vps_core(member, ctx.author, setup, prog)

@bot.command(name="viewothervps")
@has_admin_role()
async def cmd_viewothervps(ctx):
    data = load_data()
    if not data:
        await bot_send(ctx.channel, "❌ No VPS records."); return
    found = False
    for uid, udata in data.items():
        vps_list = udata.get("vps_list", [])
        if not vps_list: continue
        found = True
        try:
            user = await bot.fetch_user(int(uid))
            uname = user.display_name
        except Exception:
            uname = f"User {uid}"
        embed = discord.Embed(title=f"🖥️ {uname}'s VPS ({len(vps_list)})", color=0x5865F2)
        for vps in vps_list:
            running = await docker_status(vps["container_name"])
            embed.add_field(
                name=f"#{vps['index']} {vps['container_name']}",
                value=(
                    f"{'🟢 Running' if running else '🔴 Stopped'} • {vps.get('os','?')}\n"
                    f"IP: `{vps.get('ip','?')}` • User: `{vps.get('username','?')}`\n"
                    f"Created by: <@{vps['created_by']}>"
                ),
                inline=False,
            )
        m = await ctx.send(embed=embed)
        all_messages.setdefault(ctx.channel.id, []).append(("bot", m.id))
    if not found:
        await bot_send(ctx.channel, "❌ No VPS found for any user.")

@bot.command(name="userdeletevps")
@has_admin_role()
async def cmd_userdeletevps(ctx, user_id: int, index: int = 1):
    data = load_data()
    user_key = str(user_id)
    vps = get_vps(data, user_key, index)
    if not vps:
        await bot_send(ctx.channel, f"❌ VPS #{index} not found for `{user_id}`."); return
    ct = vps["container_name"]
    await bot_send(ctx.channel, f"🗑️ Deleting `{ct}`…")
    await run(["docker", "kill", ct])
    await asyncio.sleep(1)
    rc, _, err = await run(["docker", "rm", "-f", ct])
    if rc == 0:
        data[user_key]["vps_list"] = [v for v in data[user_key]["vps_list"] if v["index"] != index]
        save_data(data)
        try:
            u = await bot.fetch_user(user_id)
            dm = u.dm_channel or await u.create_dm()
            await dm.send(f"⚠️ Your VPS **#{index} `{ct}`** was deleted by an admin.")
        except Exception: pass
        await bot_send(ctx.channel, f"✅ VPS `{ct}` deleted.")
    else:
        await bot_send(ctx.channel, f"❌ Failed:\n```{err[:400]}```")

@bot.command(name="deletevps")
@has_admin_role()
async def cmd_deletevps(ctx, index: int = 1):
    data = load_data()
    user_key = str(ctx.author.id)
    vps = get_vps(data, user_key, index)
    if not vps:
        await bot_send(ctx.channel, f"❌ VPS #{index} not found."); return
    ct = vps["container_name"]
    await bot_send(ctx.channel, f"🗑️ Deleting `{ct}`…")
    await run(["docker", "kill", ct])
    await asyncio.sleep(1)
    rc, _, err = await run(["docker", "rm", "-f", ct])
    if rc == 0:
        data[user_key]["vps_list"] = [v for v in data[user_key]["vps_list"] if v["index"] != index]
        save_data(data)
        await bot_send(ctx.channel, f"✅ VPS `{ct}` deleted.")
    else:
        await bot_send(ctx.channel, f"❌ Failed:\n```{err[:400]}```")

@bot.command(name="status")
@has_admin_role()
async def cmd_status(ctx, index: int = 1):
    data = load_data()
    vps = get_vps(data, str(ctx.author.id), index)
    if not vps:
        await bot_send(ctx.channel, f"❌ VPS #{index} not found."); return
    ct = vps["container_name"]
    running = await docker_status(ct)
    ip = await docker_ip(ct) if running else vps.get("ip", "?")
    embed = discord.Embed(
        title=f"📊 VPS #{index} Status — `{ct}`",
        color=0x57F287 if running else 0xED4245
    )
    embed.add_field(name="Status",    value="🟢 Running" if running else "🔴 Stopped", inline=True)
    embed.add_field(name="OS",        value=vps.get("os", "?"),                         inline=True)
    embed.add_field(name="IP",        value=f"`{ip}`",                                  inline=True)
    embed.add_field(name="Username",  value=f"`{vps.get('username','?')}`",             inline=True)
    embed.add_field(name="RAM",       value=f"{VPS_RAM_MB} MB",                         inline=True)
    embed.add_field(name="CPU",       value=f"{VPS_CPUS} vCPU",                         inline=True)
    m = await ctx.send(embed=embed)
    all_messages.setdefault(ctx.channel.id, []).append(("bot", m.id))

@bot.command(name="vpslist")
@has_admin_role()
async def cmd_vpslist(ctx):
    data = load_data()
    vps_list = data.get(str(ctx.author.id), {}).get("vps_list", [])
    if not vps_list:
        await bot_send(ctx.channel, "❌ You have no VPS."); return
    embed = discord.Embed(title=f"Your VPS ({len(vps_list)} total)", color=0x5865F2)
    for vps in vps_list:
        running = await docker_status(vps["container_name"])
        embed.add_field(
            name=f"#{vps['index']} {vps['container_name']}",
            value=(
                f"{'🟢 Running' if running else '🔴 Stopped'} • {vps.get('os','?')}\n"
                f"IP: `{vps.get('ip','?')}` • User: `{vps.get('username','?')}`"
            ),
            inline=False,
        )
    m = await ctx.send(embed=embed)
    all_messages.setdefault(ctx.channel.id, []).append(("bot", m.id))

@bot.command(name="sshx")
@has_admin_role()
async def cmd_sshx(ctx, index: int = 1):
    data = load_data()
    vps = get_vps(data, str(ctx.author.id), index)
    if not vps:
        await bot_send(ctx.channel, f"❌ VPS #{index} not found."); return
    ct = vps["container_name"]
    msg = await bot_send(ctx.channel, f"⏳ Setting up sshx on `{ct}`…")
    await ensure_curl(ct)
    await msg.edit(content="⏳ Installing sshx…")
    rc, _, err = await docker_sh(ct, "curl -sSf https://sshx.io/get | sh")
    if rc != 0:
        await msg.edit(content=f"❌ sshx install failed:\n```{err[:400]}```"); return
    await msg.edit(content="⏳ Starting sshx…")
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", ct, "bash", "-c", "sshx 2>&1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    link = None
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
        except asyncio.TimeoutError: continue
        if not line: break
        m = re.search(r"https://sshx\.io/s/\S+", line.decode(errors="replace"))
        if m: link = m.group(0); break
    if proc.returncode is None:
        try: proc.kill()
        except Exception: pass
    await docker_sh(ct, "nohup sshx > /tmp/sshx.log 2>&1 &")
    if not link:
        await asyncio.sleep(4)
        _, log, _ = await docker_sh(ct, "cat /tmp/sshx.log")
        m = re.search(r"https://sshx\.io/s/\S+", log)
        if m: link = m.group(0)
    if not link:
        await msg.edit(content="❌ Could not grab sshx link. Try again."); return
    try:
        await ctx.author.send(f"✅ sshx running!\n🔗 {link}")
        await msg.edit(content="✅ sshx ready! 📬 Link sent to DMs.")
    except discord.Forbidden:
        await msg.edit(content=f"✅ sshx running!\n🔗 {link}")

@bot.command(name="tmate")
@has_admin_role()
async def cmd_tmate(ctx, index: int = 1):
    data = load_data()
    vps = get_vps(data, str(ctx.author.id), index)
    if not vps:
        await bot_send(ctx.channel, f"❌ VPS #{index} not found."); return
    ct = vps["container_name"]
    msg = await bot_send(ctx.channel, f"⏳ Setting up tmate on `{ct}`…")
    await docker_pkg(ct, "tmate")
    await docker_sh(ct, "pkill -9 tmate 2>/dev/null; rm -f /tmp/tmate.sock; true")
    await asyncio.sleep(1)
    await docker_sh(ct, "tmate -S /tmp/tmate.sock new-session -d 2>/dev/null || true")
    await asyncio.sleep(8)
    ssh_lines, web_links = [], []
    for _ in range(5):
        _, out, _ = await docker_sh(ct, "tmate -S /tmp/tmate.sock show-messages 2>&1 || true")
        ssh_lines = re.findall(r"ssh\s+\S+@\S+(?:\s+-p\s+\d+)?", out)
        web_links = re.findall(r"https://tmate\.io/t/\S+", out)
        if ssh_lines or web_links: break
        await asyncio.sleep(3)
    if not ssh_lines:
        _, dm2, _ = await docker_sh(ct, "tmate -S /tmp/tmate.sock display-message -p '#{tmate_ssh}' 2>/dev/null || true")
        if dm2.strip(): ssh_lines.append(dm2.strip())
    if not web_links:
        _, dm3, _ = await docker_sh(ct, "tmate -S /tmp/tmate.sock display-message -p '#{tmate_web}' 2>/dev/null || true")
        if dm3.strip(): web_links.append(dm3.strip())
    if not ssh_lines and not web_links:
        await msg.edit(content="❌ tmate failed. Try `!sshx` instead."); return
    lines = ["🖥️ **tmate session:**\n"]
    for s in ssh_lines: lines.append(f"```{s}```")
    for w in web_links: lines.append(f"🌐 {w}")
    try:
        await ctx.author.send("\n".join(lines))
        await msg.edit(content="✅ tmate running! 📬 Details in DMs.")
    except discord.Forbidden:
        await msg.edit(content="\n".join(lines))

@bot.command(name="clear")
@has_admin_role()
async def cmd_clear(ctx):
    entries = all_messages.get(ctx.channel.id, [])
    deleted = 0
    for _, mid in list(entries):
        try:
            m = await ctx.channel.fetch_message(mid)
            await m.delete(); deleted += 1
        except Exception: pass
        await asyncio.sleep(0.4)
    all_messages[ctx.channel.id] = []
    try: await ctx.message.delete()
    except Exception: pass
    m = await ctx.send(f"🧹 Cleared {deleted} message(s).")
    await asyncio.sleep(3)
    try: await m.delete()
    except Exception: pass

@bot.command(name="talk")
@has_admin_role()
async def cmd_talk(ctx, user_id: int):
    if ctx.author.id in active_talks:
        await bot_send(ctx.channel, "⚠️ Already in a talk. Use `!endtalk` first."); return
    try:
        target = await bot.fetch_user(user_id)
    except Exception:
        await bot_send(ctx.channel, "❌ User not found."); return
    active_talks[ctx.author.id] = user_id
    active_talks[user_id] = ctx.author.id
    try:
        dm = target.dm_channel or await target.create_dm()
        await dm.send(
            f"📞 **Admin Connected**\n"
            f"An admin has connected to chat with you.\n"
            f"Reply using `!say <message>` — type `!endtalk` to end."
        )
    except discord.Forbidden:
        await bot_send(ctx.channel, f"❌ Can't DM {target.display_name}.")
        active_talks.pop(ctx.author.id, None); active_talks.pop(user_id, None); return
    await bot_send(ctx.channel, f"✅ Connected to **{target.display_name}**. Use `!say <msg>`, `!endtalk` to stop.")

@bot.command(name="talk2")
@has_admin_role()
async def cmd_talk2(ctx, user_id: int):
    if ctx.author.id in active_talks:
        await bot_send(ctx.channel, "⚠️ Already in a talk. Use `!endtalk` first."); return
    try:
        target = await bot.fetch_user(user_id)
    except Exception:
        await bot_send(ctx.channel, "❌ User not found."); return
    active_talks[ctx.author.id] = user_id
    active_talks[user_id] = ctx.author.id
    try:
        dm = target.dm_channel or await target.create_dm()
        await dm.send(
            f"📞 **Admin Message**\n"
            f"An admin has reached out to you directly.\n"
            f"Reply using `!say <message>`"
        )
    except discord.Forbidden:
        await bot_send(ctx.channel, f"❌ Can't DM {target.display_name}.")
        active_talks.pop(ctx.author.id, None); active_talks.pop(user_id, None); return
    await bot_send(ctx.channel, f"✅ Force-connected to **{target.display_name}**. Use `!say <msg>`, `!endtalk` to stop.")

@bot.command(name="endtalk")
async def cmd_endtalk(ctx):
    uid = ctx.author.id
    if uid not in active_talks:
        msg = "❌ No active talk."
        if ctx.guild: await bot_send(ctx.channel, msg)
        else: await ctx.send(msg)
        return
    partner_id = active_talks.pop(uid)
    active_talks.pop(partner_id, None)
    try:
        partner = await bot.fetch_user(partner_id)
        dm = partner.dm_channel or await partner.create_dm()
        await dm.send("📵 The chat session has ended.")
    except Exception: pass
    if ctx.guild: await bot_send(ctx.channel, "✅ Talk ended.")
    else: await ctx.send("✅ Talk ended.")

@bot.command(name="commands")
async def cmd_commands(ctx):
    if isinstance(ctx.channel, discord.DMChannel):
        embed = discord.Embed(title="🖥️ Your VPS Commands (DM)", color=0x5865F2)
        embed.add_field(name="!sshx [#]",   value="Get browser terminal link",         inline=False)
        embed.add_field(name="!tmate [#]",  value="Get SSH session details",            inline=False)
        embed.add_field(name="!start [#]",  value="Start your VPS",                     inline=False)
        embed.add_field(name="!stop [#]",   value="Stop your VPS",                      inline=False)
        embed.add_field(name="!listvps",    value="List your VPS",                       inline=False)
        embed.add_field(name="!support",    value="Request admin support",               inline=False)
        embed.add_field(name="!say <msg>",  value="Send message during support chat",    inline=False)
        embed.add_field(name="!endtalk",    value="End a support chat",                  inline=False)
        await ctx.send(embed=embed)
        return
    embed = discord.Embed(title="🖥️ VPS Bot — Admin Commands", color=0x5865F2)
    embed.add_field(name="!createvps",              value="Create your own VPS (wizard)",                  inline=False)
    embed.add_field(name="!createsomeonevps @user", value="Create VPS for someone (they do setup in DMs)", inline=False)
    embed.add_field(name="!viewothervps",           value="View ALL users' VPS",                           inline=False)
    embed.add_field(name="!userdeletevps <id> [#]", value="Delete a specific user's VPS",                  inline=False)
    embed.add_field(name="!sshx [#]",               value="Browser terminal via sshx",                    inline=False)
    embed.add_field(name="!tmate [#]",              value="SSH via tmate",                                 inline=False)
    embed.add_field(name="!start / !stop [#]",      value="Start or stop a VPS",                          inline=False)
    embed.add_field(name="!deletevps [#]",          value="Delete your VPS",                               inline=False)
    embed.add_field(name="!status [#]",             value="VPS status embed",                              inline=False)
    embed.add_field(name="!vpslist",                value="Your VPS list",                                 inline=False)
    embed.add_field(name="!talk <id>",              value="Open support chat with a user",                 inline=False)
    embed.add_field(name="!talk2 <id>",             value="Force-open chat with any user",                 inline=False)
    embed.add_field(name="!endtalk",                value="End active chat",                               inline=False)
    embed.add_field(name="!say <msg>",              value="Send message in active chat",                   inline=False)
    embed.add_field(name="!clear",                  value="Clear all bot+user messages in channel",        inline=False)
    embed.set_footer(text="Users DM the bot: !support !start !stop !sshx !tmate !listvps !commands")
    m = await ctx.send(embed=embed)
    all_messages.setdefault(ctx.channel.id, []).append(("bot", m.id))

# ═══════════════════════════ DM HANDLER ═══════════════════════════════════

async def handle_dm(message):
    content = message.content.strip()
    uid = message.author.id
    parts = content.split()
    cmd = parts[0][1:].lower() if parts else ""
    args = parts[1:]

    # !say
    if cmd == "say":
        if uid not in active_talks:
            await message.channel.send("❌ You're not in an active chat."); return
        partner_id = active_talks[uid]
        text = " ".join(args) if args else "(empty)"
        try:
            partner = await bot.fetch_user(partner_id)
            dm = partner.dm_channel or await partner.create_dm()
            data = load_data()
            label = "👤 **User**" if str(uid) in data else "👨‍💼 **Admin**"
            await dm.send(f"{label} **{message.author.display_name}** said:\n{text}")
            await message.channel.send("✅ Sent.")
        except Exception as e:
            await message.channel.send(f"❌ Could not send: {e}")
        return

    # !endtalk
    if cmd == "endtalk":
        if uid not in active_talks:
            await message.channel.send("❌ No active talk."); return
        partner_id = active_talks.pop(uid)
        active_talks.pop(partner_id, None)
        try:
            partner = await bot.fetch_user(partner_id)
            dm = partner.dm_channel or await partner.create_dm()
            await dm.send("📵 Chat session ended.")
        except Exception: pass
        await message.channel.send("✅ Talk ended.")
        return

    # !support
    if cmd == "support":
        if uid in active_talks:
            await message.channel.send("⚠️ You're already chatting with an admin!"); return
        support_queue[uid] = True
        await message.channel.send(
            "✅ **Support Requested!**\n"
            "An admin will reach out shortly. Please describe your issue and wait."
        )
        for guild in bot.guilds:
            notified = set()
            for role_id in (SUPPORT_ROLE_ID, ADMIN_ROLE_ID):
                role = guild.get_role(role_id)
                if not role: continue
                for member in role.members:
                    if member.id in notified or member.bot: continue
                    notified.add(member.id)
                    try:
                        adm_dm = member.dm_channel or await member.create_dm()
                        await adm_dm.send(
                            f"🆘 **Support Request!**\n"
                            f"**{message.author.display_name}** (`{uid}`) needs help.\n\n"
                            f"Use `!talk {uid}` in the server to connect."
                        )
                    except Exception: pass
        return

    # !commands
    if cmd == "commands":
        embed = discord.Embed(title="🖥️ Your VPS Commands (DM)", color=0x5865F2)
        embed.add_field(name="!sshx [#]",   value="Get browser terminal link",         inline=False)
        embed.add_field(name="!tmate [#]",  value="Get SSH session details",            inline=False)
        embed.add_field(name="!start [#]",  value="Start your VPS",                     inline=False)
        embed.add_field(name="!stop [#]",   value="Stop your VPS",                      inline=False)
        embed.add_field(name="!listvps",    value="List your VPS",                       inline=False)
        embed.add_field(name="!support",    value="Request admin support",               inline=False)
        embed.add_field(name="!say <msg>",  value="Send message during support chat",    inline=False)
        embed.add_field(name="!endtalk",    value="End a support chat",                  inline=False)
        await message.channel.send(embed=embed)
        return

    data = load_data()
    user_key = str(uid)
    index = int(args[0]) if args and args[0].isdigit() else 1

    # !listvps
    if cmd == "listvps":
        vps_list = data.get(user_key, {}).get("vps_list", [])
        if not vps_list: await message.channel.send("❌ You have no VPS."); return
        embed = discord.Embed(title=f"Your VPS ({len(vps_list)} total)", color=0x5865F2)
        for vps in vps_list:
            running = await docker_status(vps["container_name"])
            embed.add_field(
                name=f"#{vps['index']} {vps['container_name']}",
                value=(
                    f"{'🟢 Running' if running else '🔴 Stopped'} • {vps.get('os','?')}\n"
                    f"IP: `{vps.get('ip','?')}` • User: `{vps.get('username','?')}`"
                ),
                inline=False,
            )
        await message.channel.send(embed=embed)
        return

    vps = get_vps(data, user_key, index)

    # !start
    if cmd == "start":
        if not vps: await message.channel.send(f"❌ VPS #{index} not found."); return
        rc, _, err = await run(["docker", "start", vps["container_name"]])
        if rc == 0:
            vps["status"] = "running"; save_data(data)
            await message.channel.send(f"▶️ `{vps['container_name']}` started!")
        else:
            await message.channel.send(f"❌ Failed:\n```{err[:400]}```")
        return

    # !stop
    if cmd == "stop":
        if not vps: await message.channel.send(f"❌ VPS #{index} not found."); return
        rc, _, err = await run(["docker", "stop", vps["container_name"]])
        if rc == 0:
            vps["status"] = "stopped"; save_data(data)
            await message.channel.send(f"⏹️ `{vps['container_name']}` stopped.")
        else:
            await message.channel.send(f"❌ Failed:\n```{err[:400]}```")
        return

    # !sshx (DM)
    if cmd == "sshx":
        if not vps: await message.channel.send(f"❌ VPS #{index} not found."); return
        ct = vps["container_name"]
        msg = await message.channel.send(f"⏳ Setting up sshx on `{ct}`…")
        await ensure_curl(ct)
        await msg.edit(content="⏳ Installing sshx…")
        rc, _, err = await docker_sh(ct, "curl -sSf https://sshx.io/get | sh")
        if rc != 0: await msg.edit(content=f"❌ sshx failed:\n```{err[:400]}```"); return
        await msg.edit(content="⏳ Starting sshx…")
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", ct, "bash", "-c", "sshx 2>&1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        link = None
        deadline = asyncio.get_event_loop().time() + 30
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
            except asyncio.TimeoutError: continue
            if not line: break
            m = re.search(r"https://sshx\.io/s/\S+", line.decode(errors="replace"))
            if m: link = m.group(0); break
        if proc.returncode is None:
            try: proc.kill()
            except Exception: pass
        await docker_sh(ct, "nohup sshx > /tmp/sshx.log 2>&1 &")
        if not link:
            await asyncio.sleep(4)
            _, log, _ = await docker_sh(ct, "cat /tmp/sshx.log")
            m = re.search(r"https://sshx\.io/s/\S+", log)
            if m: link = m.group(0)
        if not link: await msg.edit(content="❌ Could not grab link. Try again."); return
        await msg.edit(content=f"✅ sshx running!\n🔗 {link}")
        return

    # !tmate (DM)
    if cmd == "tmate":
        if not vps: await message.channel.send(f"❌ VPS #{index} not found."); return
        ct = vps["container_name"]
        msg = await message.channel.send(f"⏳ Setting up tmate on `{ct}`…")
        await docker_pkg(ct, "tmate")
        await docker_sh(ct, "pkill -9 tmate 2>/dev/null; rm -f /tmp/tmate.sock; true")
        await asyncio.sleep(1)
        await docker_sh(ct, "tmate -S /tmp/tmate.sock new-session -d 2>/dev/null || true")
        await asyncio.sleep(8)
        ssh_lines, web_links = [], []
        for _ in range(5):
            _, out, _ = await docker_sh(ct, "tmate -S /tmp/tmate.sock show-messages 2>&1 || true")
            ssh_lines = re.findall(r"ssh\s+\S+@\S+(?:\s+-p\s+\d+)?", out)
            web_links = re.findall(r"https://tmate\.io/t/\S+", out)
            if ssh_lines or web_links: break
            await asyncio.sleep(3)
        if not ssh_lines:
            _, dm2, _ = await docker_sh(ct, "tmate -S /tmp/tmate.sock display-message -p '#{tmate_ssh}' 2>/dev/null || true")
            if dm2.strip(): ssh_lines.append(dm2.strip())
        if not web_links:
            _, dm3, _ = await docker_sh(ct, "tmate -S /tmp/tmate.sock display-message -p '#{tmate_web}' 2>/dev/null || true")
            if dm3.strip(): web_links.append(dm3.strip())
        if not ssh_lines and not web_links:
            await msg.edit(content="❌ tmate failed. Try `!sshx` instead."); return
        lines = ["🖥️ **tmate session:**\n"]
        for s in ssh_lines: lines.append(f"```{s}```")
        for w in web_links: lines.append(f"🌐 {w}")
        await msg.edit(content="\n".join(lines))
        return

    await message.channel.send(f"❓ Unknown command `!{cmd}`. Type `!commands` to see available commands.")

# ═══════════════════════════ EVENT ROUTER ═════════════════════════════════

@bot.event
async def on_message(message):
    if message.author.bot: return
    if isinstance(message.channel, discord.DMChannel):
        if message.content.startswith("!"):
            await handle_dm(message)
        return
    all_messages.setdefault(message.channel.id, []).append(("user", message.id))
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ User not found.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing argument. Try `!commands`.")
    elif isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
        pass
    else:
        await ctx.send(f"❌ Error: `{error}`")

@bot.event
async def on_ready():
    print(f"✅ Bot online: {bot.user} ({bot.user.id})")

bot.run(BOT_TOKEN)
