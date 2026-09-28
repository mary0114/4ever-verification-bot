import os
import io
import json
import asyncio
from datetime import datetime, timezone, timedelta

import aiohttp
from PIL import Image
import discord
from discord import app_commands
from discord.ext import commands

# =========================================================
# 환경변수
# =========================================================
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID_RAW = os.getenv("OWNER_ID", "")
GUILD_ID_RAW = os.getenv("GUILD_ID", "")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다.")
if not OWNER_ID_RAW:
    raise RuntimeError("OWNER_ID 환경변수가 설정되지 않았습니다.")

OWNER_ID = int(OWNER_ID_RAW)
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW else None

# =========================================================
# 영구 데이터 저장
# =========================================================
# Railway Volume이 연결되어 있으면 RAILWAY_VOLUME_MOUNT_PATH를 사용합니다.
# Volume이 없을 경우 /data를 기본 경로로 사용합니다.
# 중요: Railway에서 반드시 Volume을 서비스에 연결하고 Mount Path를 지정해야
# 재배포/재시작 후에도 코인 데이터가 유지됩니다.
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR", "/data")
DATA_FILE = os.path.join(DATA_DIR, "verification_data.json")

print(f"[DATA] 저장 경로: {DATA_FILE}")

# 승인/반려를 할 수 있는 역할 ID
VERIFICATION_ROLE_ID = 1534583787856330842

VERIFICATION_TYPES = [
    "추천 인증",
    "후기 작성 인증",
    "초대 인증",
    "부계정 초대 인증",
    "이벤트 참여 인증",
    "구매 인증",
]

REWARDS = {
    "추천 인증": 1,
    "후기 작성 인증": 1,
    "초대 인증": 2,
    "부계정 초대 인증": 2,
    "이벤트 참여 인증": 1,
    "구매 인증": 0,
}

# 신청 하나를 동시에 두 번 처리하지 못하게 하는 잠금입니다.
REQUEST_LOCKS = {}


def get_request_lock(request_id):
    lock = REQUEST_LOCKS.get(request_id)
    if lock is None:
        lock = asyncio.Lock()
        REQUEST_LOCKS[request_id] = lock
    return lock


def default_data():
    return {
        "threads": {name: None for name in VERIFICATION_TYPES},
        "requests": {},
        "coins": {},
        "used_invite_members": {},
        "log_channel_id": None,
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        print("[DATA] 기존 데이터 파일이 없어 새 데이터 파일을 생성합니다.")
        return default_data()

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("데이터 파일 형식이 올바르지 않습니다.")
    except Exception as e:
        # 데이터 파일이 깨졌을 때 코인을 초기화해버리지 않도록 합니다.
        # 봇을 중단시켜 기존 파일을 보존하고, 로그에 원인을 남깁니다.
        raise RuntimeError(
            f"verification_data.json을 읽을 수 없습니다. 기존 코인 데이터를 보호하기 위해 봇을 시작하지 않습니다: {e}"
        ) from e

    base = default_data()
    for key in base:
        if isinstance(loaded.get(key), dict):
            base[key].update(loaded[key])
    return base


data = load_data()


def save_data_sync():
    os.makedirs(DATA_DIR, exist_ok=True)
    temp = DATA_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, DATA_FILE)


async def save_data():
    # JSON 저장 때문에 Discord 이벤트 루프가 멈추지 않도록 별도 스레드에서 저장합니다.
    await asyncio.to_thread(save_data_sync)


def now_kst():
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def make_request_id():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def can_process_verification(user):
    return isinstance(user, discord.Member) and any(role.id == VERIFICATION_ROLE_ID for role in user.roles)


def is_admin(user):
    if user.id == OWNER_ID:
        return True
    return isinstance(user, discord.Member) and (
        user.guild_permissions.administrator
        or user.guild_permissions.manage_guild
    )


def get_coin(user_id):
    return int(data["coins"].get(str(user_id), 0))


def add_coin(user_id, amount):
    key = str(user_id)
    data["coins"][key] = get_coin(user_id) + amount
    return data["coins"][key]


def subtract_coin(user_id, amount):
    key = str(user_id)
    balance = get_coin(user_id)
    if amount < 0 or balance < amount:
        return None
    data["coins"][key] = balance - amount
    return data["coins"][key]


async def get_configured_thread(guild, verification_type):
    raw_id = data["threads"].get(verification_type)
    if not raw_id:
        return None
    try:
        channel = guild.get_channel_or_thread(int(raw_id))
    except (TypeError, ValueError):
        return None
    return channel if isinstance(channel, discord.Thread) else None


def get_participant_thread(guild, request):
    raw_id = request.get("source_thread_id")
    if not raw_id:
        return None
    try:
        channel = guild.get_channel_or_thread(int(raw_id))
    except (TypeError, ValueError):
        return None
    return channel if isinstance(channel, discord.Thread) else None


async def send_log(guild, *, action, admin, request=None, target=None, amount=None, balance=None, reason=None):
    raw_id = data.get("log_channel_id")
    if not raw_id:
        return
    try:
        channel = guild.get_channel(int(raw_id))
    except (TypeError, ValueError):
        return
    if channel is None:
        try:
            channel = await guild.fetch_channel(int(raw_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            return

    embed = discord.Embed(
        title="﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒\n4ever 활동 로그",
        color=discord.Color.from_rgb(184, 163, 255),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="관리자", value=admin.mention, inline=False)
    embed.add_field(name="내용", value=action, inline=False)
    if request:
        embed.add_field(name="인증 보낸 사람", value=f"<@{request.get('user_id')}>", inline=True)
        embed.add_field(name="인증 종류", value=str(request.get("type", "-")), inline=True)
        embed.add_field(name="신청 ID", value=str(request.get("request_id", "-")), inline=False)
    if target is not None:
        embed.add_field(name="대상", value=target.mention, inline=True)
    if amount is not None:
        prefix = "+" if amount > 0 else ""
        embed.add_field(name="코인 변동", value=f"{prefix}{amount} 코인", inline=True)
    if balance is not None:
        embed.add_field(name="처리 후 보유 코인", value=f"{balance} 코인", inline=True)
    if reason:
        embed.add_field(name="반려 사유", value=reason, inline=False)
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
        print(f"[WARN] 로그 전송 실패: {e}")


def request_id_from_message(message):
    if not message or not message.embeds:
        return None
    footer = message.embeds[0].footer.text or ""
    prefix = "신청 ID: "
    if footer.startswith(prefix):
        return footer[len(prefix):].strip()
    return None


def build_request_embed(request_id, request_type, user, fields, status="⏳ 승인 대기", color=None, image_filename=None):
    embed = discord.Embed(
        title=f"﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒\n{request_type}",
        description=f"📨 인증 보낸 사람: {user.mention}",
        color=color or discord.Color.from_rgb(184, 163, 255),
    )
    for name, value in fields:
        embed.add_field(name=name, value=str(value), inline=False)
    embed.add_field(name="상태", value=status, inline=False)
    if image_filename:
        embed.set_image(url=f"attachment://{image_filename}")
    embed.set_footer(text=f"신청 ID: {request_id}")
    return embed


async def make_collage(attachments):
    if len(attachments) == 1:
        file = await attachments[0].to_file()
        filename = "verification_image.png"
        # 원본이 jpg/webp여도 Discord attachment URL 이름을 고정합니다.
        # Pillow로 PNG로 다시 저장하여 attachment:// 렌더링을 확실하게 합니다.
        raw = await attachments[0].read()
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        output.seek(0)
        return discord.File(output, filename=filename), filename

    images = []
    async with aiohttp.ClientSession() as session:
        for attachment in attachments[:2]:
            async with session.get(attachment.url) as response:
                if response.status != 200:
                    raise RuntimeError(f"이미지 다운로드 실패: HTTP {response.status}")
                raw = await response.read()
            images.append(Image.open(io.BytesIO(raw)).convert("RGB"))

    target_height = 700
    resized = []
    for image in images:
        ratio = target_height / max(1, image.height)
        width = max(1, int(image.width * ratio))
        resized.append(image.resize((width, target_height), Image.Resampling.LANCZOS))

    gap = 8
    canvas = Image.new(
        "RGB",
        (resized[0].width + gap + resized[1].width, target_height),
        "white",
    )
    canvas.paste(resized[0], (0, 0))
    canvas.paste(resized[1], (resized[0].width + gap, 0))

    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    output.seek(0)
    return discord.File(output, filename="verification_photos.png"), "verification_photos.png"


async def send_admin_request(guild, request_id, request_type, embed, file=None):
    thread = await get_configured_thread(guild, request_type)
    if thread is None:
        return None
    return await thread.send(
        embed=embed,
        file=file,
        view=AdminRequestView(),
    )


async def send_participant_result(guild, request, *, approved, reward=0, balance=0, reason=None):
    thread = get_participant_thread(guild, request)
    if thread is None:
        print(
            f"[WARN] 참여자 스레드 없음: request={request.get('request_id')}, "
            f"source_thread_id={request.get('source_thread_id')}"
        )
        return

    request_type = request["type"]
    if approved:
        if request_type == "구매 인증":
            spent = int(request.get("coins_deducted", request.get("coins", 0)) or 0)
            description = (
                f"**{request_type}이 승인되었습니다.**\n\n"
                f"🪙 **코인 사용**\n"
                f"-{spent} 코인 · 현재 {balance} 코인"
            )
        else:
            description = (
                f"**{request_type}이 승인되었습니다.**\n\n"
                f"🪙 **코인 지급**\n"
                f"+{reward} 코인 · 현재 {balance} 코인"
            )
        embed = discord.Embed(
            title="﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒\n인증 완료",
            description=description,
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            description=(
                "**﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒**\n"
                "[❌](https://discord.com/assets/4f584fe7b12fcf02.svg) 반려\n"
                "**반려 사유**\n"
                f": {reason}\n"
                "**﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒**"
            ),
            color=discord.Color.red(),
        )

    if approved:
        embed.set_footer(text=f"신청 ID: {request.get('request_id', '')}")
    try:
        await thread.send(embed=embed)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
        print(f"[WARN] 참여자 스레드 결과 전송 실패: {e}")


async def send_user_dm(guild, request, approved, reward=0, balance=0, reason=None):
    member = guild.get_member(int(request["user_id"]))
    if not member:
        return
    try:
        if approved:
            text = (
                f"✅ **{request['type']}** 인증이 승인되었습니다.\n"
                f"🪙 +{reward} 코인 · 현재 {balance} 코인"
                if reward > 0
                else f"✅ **{request['type']}** 인증이 승인되었습니다."
            )
        else:
            text = (
                f"❌ **{request['type']}** 인증이 반려되었습니다.\n"
                f"반려 사유: {reason}"
            )
        await member.send(text)
    except (discord.Forbidden, discord.HTTPException):
        pass


# =========================================================
# 사진 인증
# =========================================================
class PhotoVerificationModal(discord.ui.Modal):
    def __init__(self, request_type):
        super().__init__(title=request_type)
        self.request_type = request_type

        self.file_upload = discord.ui.FileUpload(
            custom_id=f"verification:{request_type}:files",
            min_values=1,
            max_values=2,
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text="인증 사진",
                description="인증 사진을 1~2장 첨부해주세요.",
                component=self.file_upload,
            )
        )

    async def on_submit(self, interaction):
        attachments = list(self.file_upload.values or [])
        if not attachments:
            await interaction.response.send_message("❌ 사진을 1장 이상 첨부해주세요.", ephemeral=True)
            return

        for attachment in attachments[:2]:
            if not (attachment.content_type or "").startswith("image/"):
                await interaction.response.send_message("❌ 이미지 파일만 첨부할 수 있어요.", ephemeral=True)
                return

        # 모달 제출에 대한 응답을 먼저 확보합니다.
        await interaction.response.defer(ephemeral=True)

        try:
            file, filename = await make_collage(attachments[:2])
            request_id = make_request_id()
            source_thread_id = interaction.channel.id if isinstance(interaction.channel, discord.Thread) else None

            request = {
                "request_id": request_id,
                "type": self.request_type,
                "user_id": interaction.user.id,
                "status": "pending",
                "created_at": now_kst(),
                "source_thread_id": source_thread_id,
                "original_attachment_urls": [a.url for a in attachments[:2]],
            }
            data["requests"][request_id] = request

            embed = build_request_embed(
                request_id,
                self.request_type,
                interaction.user,
                [("📷 인증 사진", "임베드에서 인증 사진을 확인해주세요.")],
                image_filename=filename,
            )

            admin_message = await send_admin_request(
                interaction.guild,
                request_id,
                self.request_type,
                embed,
                file=file,
            )

            if admin_message is None:
                data["requests"].pop(request_id, None)
                await save_data()
                await interaction.followup.send(
                    f"⚠️ **{self.request_type}** 인증 스레드가 설정되지 않았어요.\n관리자가 `/인증설정`으로 연결해주세요.",
                    ephemeral=True,
                )
                return

            request["admin_message_id"] = admin_message.id
            request["admin_thread_id"] = admin_message.channel.id
            await save_data()

            await interaction.followup.send(
                "✅ 인증이 정상적으로 접수되었습니다.\n관리자 확인 후 결과를 알려드릴게요.",
                ephemeral=True,
            )
        except Exception as e:
            print(f"[ERROR] 사진 인증 접수 실패: {e}")
            await interaction.followup.send(
                "❌ 인증 접수 중 오류가 발생했어요. 다시 시도해주세요.",
                ephemeral=True,
            )


# =========================================================
# 초대 인증 - 패널 전용, /초대인증 없음
# =========================================================
async def complete_invite(interaction, invited_name, request_type="초대 인증"):
    """패널에서 초대한 사람의 이름을 입력받아 해당 관리자 인증 스레드에 신청을 보냅니다."""
    invited_name = invited_name.strip()
    if not invited_name:
        await interaction.response.send_message(
            "❌ 초대한 사람 이름을 입력해주세요.",
            ephemeral=True,
        )
        return

    # 같은 이름의 초대 인증이 이미 처리 중인지 확인
    invited_key = invited_name.casefold()
    for existing in data["requests"].values():
        if (
            existing.get("type") == request_type
            and existing.get("status") == "pending"
            and str(existing.get("invited_member_name", "")).casefold() == invited_key
        ):
            await interaction.response.send_message(
                "⚠️ 해당 이름의 초대 인증이 이미 관리자 확인을 기다리고 있어요.",
                ephemeral=True,
            )
            return

    thread = await get_configured_thread(interaction.guild, request_type)
    if thread is None:
        await interaction.response.send_message(
            "⚠️ **초대 인증** 스레드가 아직 설정되지 않았어요.",
            ephemeral=True,
        )
        return

    request_id = make_request_id()
    request = {
        "request_id": request_id,
        "type": request_type,
        "user_id": interaction.user.id,
        "invited_member_name": invited_name,
        "status": "pending",
        "created_at": now_kst(),
        "source_thread_id": interaction.channel.id if isinstance(interaction.channel, discord.Thread) else None,
    }
    data["requests"][request_id] = request

    embed = build_request_embed(
        request_id,
        request_type,
        interaction.user,
        [
            ("👤 초대한 사람", invited_name),
            ("🪙 승인 시 지급", "+2 코인"),
        ],
    )

    try:
        message = await thread.send(
            embed=embed,
            view=AdminRequestView(),
        )
        request["admin_message_id"] = message.id
        request["admin_thread_id"] = message.channel.id
        await save_data()
        await interaction.response.send_message(
            f"✅ {request_type}이 접수되었습니다.\n관리자 확인 후 코인이 지급됩니다.",
            ephemeral=True,
        )
    except Exception as e:
        print(f"[ERROR] 초대 인증 접수 실패: {e}")
        data["requests"].pop(request_id, None)
        await save_data()
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"❌ {request_type} 접수 중 오류가 발생했어요.",
                ephemeral=True,
            )


class InviteNameModal(discord.ui.Modal):
    def __init__(self, request_type="초대 인증"):
        super().__init__(title=request_type)
        self.request_type = request_type
        self.invited_name = discord.ui.TextInput(
            label="초대한 사람 이름",
            placeholder="예: 마리",
            required=True,
            min_length=1,
            max_length=100,
        )
        self.add_item(self.invited_name)

    async def on_submit(self, interaction):
        await complete_invite(interaction, self.invited_name.value, self.request_type)


# =========================================================
# 구매 인증
# =========================================================
class PurchaseModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="직접 구매")
        self.coins = discord.ui.TextInput(
            label="코인 갯수",
            placeholder="예: 10",
            style=discord.TextStyle.short,
            required=True,
            max_length=20,
        )
        self.product = discord.ui.TextInput(
            label="원하는 제작물",
            placeholder="예: 캐릭터 배너",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
        )
        self.add_item(self.coins)
        self.add_item(self.product)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        request_id = make_request_id()
        request = {
            "request_id": request_id,
            "type": "구매 인증",
            "user_id": interaction.user.id,
            "status": "pending",
            "created_at": now_kst(),
            "coins": self.coins.value.strip(),
            "product": self.product.value.strip(),
            "source_thread_id": interaction.channel.id if isinstance(interaction.channel, discord.Thread) else None,
        }
        data["requests"][request_id] = request

        embed = build_request_embed(
            request_id,
            "구매 인증",
            interaction.user,
            [
                ("🪙 현재 보유 코인", f"{get_coin(interaction.user.id)}개"),
                ("🪙 코인 사용", request["coins"]),
                ("🎨 원하는 제작물", request["product"]),
            ],
        )
        message = await send_admin_request(interaction.guild, request_id, "구매 인증", embed)
        if message is None:
            data["requests"].pop(request_id, None)
            await save_data()
            await interaction.followup.send("⚠️ 구매 인증 스레드가 설정되지 않았어요.", ephemeral=True)
            return

        request["admin_message_id"] = message.id
        request["admin_thread_id"] = message.channel.id
        await save_data()
        await interaction.followup.send("✅ 구매 신청이 접수되었습니다.", ephemeral=True)


# =========================================================
# 반려 모달
# =========================================================
class RejectModal(discord.ui.Modal):
    def __init__(self, request_id):
        super().__init__(title="반려 사유 작성")
        self.request_id = request_id
        self.reason = discord.ui.TextInput(
            label="반려 사유",
            placeholder="반려하는 이유를 작성해주세요.",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction):
        if not can_process_verification(interaction.user):
            await interaction.response.send_message("❌ 인증 처리 권한이 없습니다.", ephemeral=True)
            return

        lock = get_request_lock(self.request_id)
        async with lock:
            request = data["requests"].get(self.request_id)
            if not request or request.get("status") != "pending":
                await interaction.response.send_message("⚠️ 이미 처리됐거나 존재하지 않는 신청입니다.", ephemeral=True)
                return

            # 반려도 가장 먼저 Discord에 응답을 확보합니다.
            await interaction.response.defer()

            reason = self.reason.value.strip()
            request["status"] = "rejected"
            request["reason"] = reason
            request["processed_at"] = now_kst()
            request["processed_by"] = interaction.user.id

            embed = interaction.message.embeds[0].copy()
            embed.color = discord.Color.red()
            embed.set_image(url=None)
            for i, field in enumerate(embed.fields):
                if field.name == "상태":
                    embed.set_field_at(i, name="상태", value="❌ 반려", inline=False)
                    break
            embed.add_field(name="❌ 반려 사유", value=reason, inline=False)

            try:
                await interaction.edit_original_response(embed=embed, view=None, attachments=[])
            except Exception as e:
                print(f"[ERROR] 반려 원본 메시지 수정 실패: {e}")

            await save_data()
            await send_participant_result(interaction.guild, request, approved=False, reason=reason)
            await send_user_dm(interaction.guild, request, approved=False, reason=reason)
            await send_log(
                interaction.guild,
                action="❌ 인증 반려",
                admin=interaction.user,
                request=request,
                reason=reason,
            )


# =========================================================
# 관리자 신청 버튼
# =========================================================
class AdminRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="승인",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="verification:admin:approve",
    )
    async def approve(self, interaction, button):
        if not can_process_verification(interaction.user):
            await interaction.response.send_message(
                "❌ 인증 처리 권한이 없습니다.",
                ephemeral=True,
            )
            return

        request_id = request_id_from_message(interaction.message)
        if not request_id:
            await interaction.response.send_message(
                "❌ 신청 ID를 찾을 수 없습니다.",
                ephemeral=True,
            )
            return

        lock = get_request_lock(request_id)
        async with lock:
            request = data["requests"].get(request_id)

            if not request or request.get("status") != "pending":
                await interaction.response.send_message(
                    "⚠️ 이미 처리됐거나 존재하지 않는 신청입니다.",
                    ephemeral=True,
                )
                return

            # defer()를 사용하지 않습니다.
            # 기존 방식처럼 interaction.response.edit_message()로
            # 버튼 클릭에 즉시 응답합니다.
            reward = REWARDS.get(request.get("type"), 0)
            old_balance = get_coin(request["user_id"])
            new_balance = old_balance

            # 구매 인증은 신청한 코인만큼 승인 시 자동 차감합니다.
            spend_amount = 0
            if request.get("type") == "구매 인증":
                try:
                    spend_amount = int(str(request.get("coins", "0")).replace(",", "").strip())
                except ValueError:
                    await interaction.response.send_message(
                        "❌ 사용 코인 수가 올바르지 않습니다.",
                        ephemeral=True,
                    )
                    return
                if spend_amount <= 0:
                    await interaction.response.send_message(
                        "❌ 사용 코인은 1개 이상 입력해주세요.",
                        ephemeral=True,
                    )
                    return
                if old_balance < spend_amount:
                    await interaction.response.send_message(
                        f"❌ 코인이 부족합니다.\n현재 코인: **{old_balance}개**\n필요 코인: **{spend_amount}개**",
                        ephemeral=True,
                    )
                    return

            request["status"] = "approved"
            request["processed_at"] = now_kst()
            request["processed_by"] = interaction.user.id

            if reward > 0 and not request.get("coins_awarded"):
                new_balance = add_coin(request["user_id"], reward)
                request["coins_awarded"] = reward
                request["coin_balance"] = new_balance
            else:
                new_balance = get_coin(request["user_id"])

            if spend_amount > 0 and not request.get("coins_deducted"):
                deducted_balance = subtract_coin(request["user_id"], spend_amount)
                if deducted_balance is None:
                    # 이론상 위의 잔액 확인으로 발생하지 않지만 안전하게 중단합니다.
                    request["status"] = "pending"
                    await interaction.response.send_message(
                        "❌ 코인 차감 중 문제가 발생했습니다. 승인되지 않았습니다.",
                        ephemeral=True,
                    )
                    return
                request["coins_deducted"] = spend_amount
                request["coin_balance_after_spend"] = deducted_balance
                new_balance = deducted_balance

            # 관리자 신청 메시지: 승인 상태로 바꾸고 사진을 완전히 제거합니다.
            if interaction.message.embeds:
                embed = interaction.message.embeds[0].copy()
            else:
                embed = discord.Embed()

            embed.color = discord.Color.green()
            embed.set_image(url=None)

            status_updated = False
            for index, field in enumerate(embed.fields):
                if field.name == "상태":
                    embed.set_field_at(
                        index,
                        name="상태",
                        value="✅ 승인",
                        inline=False,
                    )
                    status_updated = True
                    break

            if not status_updated:
                embed.add_field(
                    name="상태",
                    value="✅ 승인",
                    inline=False,
                )

            # 기존 코인 지급 표시가 있다면 제거한 뒤 현재 잔액으로 다시 표시합니다.
            for index in range(len(embed.fields) - 1, -1, -1):
                if embed.fields[index].name == "🪙 코인 지급":
                    embed.remove_field(index)

            if spend_amount > 0:
                embed.add_field(
                    name="🪙 코인 사용",
                    value=f"-{spend_amount} 코인 · 현재 {new_balance} 코인",
                    inline=False,
                )
            elif reward > 0:
                embed.add_field(
                    name="🪙 코인 지급",
                    value=f"+{reward} 코인 · 현재 {new_balance} 코인",
                    inline=False,
                )

            # ★ 중요: defer() 없이 여기서 바로 Discord에 응답합니다.
            # 이 줄이 승인 버튼 클릭에 대한 최초 응답입니다.
            await interaction.response.edit_message(
                embed=embed,
                view=None,
                attachments=[],
            )

            # Discord 응답이 끝난 뒤 저장/후속 메시지를 처리합니다.
            try:
                await save_data()
            except Exception as e:
                print(f"[ERROR] 승인 데이터 저장 실패: {e}")

            # 참여자가 인증을 제출했던 본인 스레드에 승인 완료 임베드를 보냅니다.
            await send_participant_result(
                interaction.guild,
                request,
                approved=True,
                reward=reward,
                balance=new_balance,
            )

            # DM도 전송합니다.
            await send_user_dm(
                interaction.guild,
                request,
                approved=True,
                reward=reward,
                balance=new_balance,
            )

            coin_change = 0
            if spend_amount > 0:
                coin_change = -spend_amount
            elif reward > 0:
                coin_change = reward
            await send_log(
                interaction.guild,
                action="✅ 인증 승인",
                admin=interaction.user,
                request=request,
                amount=coin_change if coin_change else None,
                balance=new_balance,
            )

    @discord.ui.button(
        label="반려",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="verification:admin:reject",
    )
    async def reject(self, interaction, button):
        if not can_process_verification(interaction.user):
            await interaction.response.send_message("❌ 인증 처리 권한이 없습니다.", ephemeral=True)
            return

        request_id = request_id_from_message(interaction.message)
        if not request_id:
            await interaction.response.send_message("❌ 신청 ID를 찾을 수 없습니다.", ephemeral=True)
            return

        request = data["requests"].get(request_id)
        if not request or request.get("status") != "pending":
            await interaction.response.send_message("⚠️ 이미 처리됐거나 존재하지 않는 신청입니다.", ephemeral=True)
            return

        await interaction.response.send_modal(RejectModal(request_id))


# =========================================================
# 사용자 인증 패널
# =========================================================
class MainVerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="추천 인증", emoji="📸", style=discord.ButtonStyle.primary, custom_id="verification:user:recommend", row=0)
    async def recommend(self, interaction, button):
        await interaction.response.send_modal(PhotoVerificationModal("추천 인증"))

    @discord.ui.button(label="후기 작성 인증", emoji="📝", style=discord.ButtonStyle.primary, custom_id="verification:user:review", row=0)
    async def review(self, interaction, button):
        await interaction.response.send_modal(PhotoVerificationModal("후기 작성 인증"))

    @discord.ui.button(label="초대 인증", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="verification:user:invite", row=1)
    async def invite(self, interaction, button):
        await interaction.response.send_modal(InviteNameModal("초대 인증"))

    @discord.ui.button(label="부계정 초대 인증", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="verification:user:altinvite", row=1)
    async def alt_invite(self, interaction, button):
        await interaction.response.send_modal(InviteNameModal("부계정 초대 인증"))

    @discord.ui.button(label="이벤트 참여 인증", emoji="🎉", style=discord.ButtonStyle.secondary, custom_id="verification:user:event", row=2)
    async def event(self, interaction, button):
        await interaction.response.send_modal(PhotoVerificationModal("이벤트 참여 인증"))


# =========================================================
# Bot
# =========================================================
class VerificationBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=discord.Intents.default(),
        )

    async def setup_hook(self):
        # 기존 메시지의 버튼은 새 custom_id를 쓰므로 새 패널을 한 번 생성해야 합니다.
        self.add_view(MainVerificationView())
        self.add_view(AdminRequestView())

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[INFO] 서버 명령어 동기화: {len(synced)}개")
        else:
            synced = await self.tree.sync()
            print(f"[INFO] 전역 명령어 동기화: {len(synced)}개")


bot = VerificationBot()


@bot.tree.command(name="인증패널", description="4ever 인증 패널을 생성합니다.")
async def verification_panel(interaction):
    embed = discord.Embed(
        title="﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒\n4ever 인증 접수",
        description=(
            "아래에서 해당하는 인증을 선택해주세요.\n\n"
            "📸 **추천 인증** — 사진 필수\n"
            "📝 **후기 작성 인증** — 사진 필수\n"
            "👥 **초대 인증** — 초대한 사람 이름 입력\n"
            "👤 **부계정 초대 인증** — 초대한 부계정 이름 입력\n"
            "🎉 **이벤트 참여 인증** — 사진 필수"
        ),
        color=discord.Color.from_rgb(184, 163, 255),
    )
    await interaction.response.defer()
    await interaction.followup.send(embed=embed, view=MainVerificationView())


@bot.tree.command(name="코인", description="현재 보유한 4ever 코인을 확인합니다.")
async def coins(interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(
        f"🪙 {interaction.user.mention}님의 현재 코인은 **{get_coin(interaction.user.id)}개**예요!",
        ephemeral=True,
    )


@bot.tree.command(name="직접구매", description="코인으로 원하는 제작물을 구매 신청합니다.")
async def direct_purchase(interaction):
    await interaction.response.send_modal(PurchaseModal())


@bot.tree.command(name="코인지급", description="멘션한 회원에게 코인을 지급합니다.")
@app_commands.describe(멘션="코인을 지급할 회원", 갯수="지급할 코인 개수")
async def give_coins(interaction, 멘션: discord.Member, 갯수: app_commands.Range[int, 1, 100000]):
    if not can_process_verification(interaction.user):
        await interaction.response.send_message(
            "❌ 코인 지급 권한이 없습니다.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    new_balance = add_coin(멘션.id, int(갯수))
    await save_data()
    await interaction.followup.send(
        f"🪙 {멘션.mention}님에게 **+{갯수} 코인**을 지급했습니다.\n"
        f"현재 보유 코인: **{new_balance}개**",
        ephemeral=True,
    )
    await send_log(
        interaction.guild,
        action="🪙 코인 지급",
        admin=interaction.user,
        target=멘션,
        amount=int(갯수),
        balance=new_balance,
    )


@bot.tree.command(name="코인차감", description="멘션한 회원의 코인을 차감합니다.")
@app_commands.describe(멘션="코인을 차감할 회원", 갯수="차감할 코인 개수")
async def take_coins(interaction, 멘션: discord.Member, 갯수: app_commands.Range[int, 1, 100000]):
    if not can_process_verification(interaction.user):
        await interaction.response.send_message(
            "❌ 코인 차감 권한이 없습니다.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    new_balance = subtract_coin(멘션.id, int(갯수))
    if new_balance is None:
        await interaction.followup.send(
            f"❌ {멘션.mention}님의 코인이 부족합니다.\n"
            f"현재 보유 코인: **{get_coin(멘션.id)}개**\n"
            f"차감하려는 코인: **{갯수}개**",
            ephemeral=True,
        )
        return

    await save_data()
    await interaction.followup.send(
        f"🪙 {멘션.mention}님에게서 **-{갯수} 코인**을 차감했습니다.\n"
        f"현재 보유 코인: **{new_balance}개**",
        ephemeral=True,
    )
    await send_log(
        interaction.guild,
        action="🪙 코인 차감",
        admin=interaction.user,
        target=멘션,
        amount=-int(갯수),
        balance=new_balance,
    )


@bot.tree.command(name="로그설정", description="4ever 활동 로그를 보낼 채널을 설정합니다.")
@app_commands.describe(채널="활동 로그를 보낼 텍스트 채널")
async def set_log_channel(interaction, 채널: discord.TextChannel):
    if interaction.user.id != OWNER_ID and not is_admin(interaction.user):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    data["log_channel_id"] = str(채널.id)
    await save_data()
    await interaction.response.send_message(
        f"✅ 활동 로그 채널을 {채널.mention}으로 설정했습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="인증설정", description="인증 종류와 스레드를 연결합니다.")
@app_commands.describe(종류="인증 종류", 스레드="해당 인증이 들어갈 스레드")
@app_commands.choices(종류=[app_commands.Choice(name=x, value=x) for x in VERIFICATION_TYPES])
async def set_thread(interaction, 종류: app_commands.Choice[str], 스레드: discord.Thread):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ 봇 소유자만 사용할 수 있습니다.", ephemeral=True)
        return
    data["threads"][종류.value] = str(스레드.id)
    await save_data()
    await interaction.response.send_message(
        f"✅ **{종류.value}** → {스레드.mention}\n설정 완료!",
        ephemeral=True,
    )


@bot.tree.command(name="인증설정확인", description="현재 인증 스레드 연결 상태를 확인합니다.")
async def show_thread_settings(interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    lines = []
    for name in VERIFICATION_TYPES:
        raw = data["threads"].get(name)
        if not raw:
            lines.append(f"• **{name}** → ❌ 미설정")
            continue
        channel = interaction.guild.get_channel_or_thread(int(raw))
        lines.append(f"• **{name}** → {channel.mention if channel else '⚠️ 스레드를 찾을 수 없음'}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.event
async def on_ready():
    print(f"[INFO] 로그인 완료: {bot.user} (ID: {bot.user.id})")


bot.run(TOKEN)
