import os
import json
import io
from datetime import datetime, timezone, timedelta

import aiohttp
from PIL import Image, ImageOps, ImageDraw

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
# 저장 경로
# Railway Volume을 /app/data 로 연결하면 자동으로 그곳에 저장됩니다.
# =========================================================
DATA_FILE = os.path.join(
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."),
    "verification_data.json",
)

THREAD_KEYS = [
    "추천 인증",
    "후기 작성 인증",
    "초대 인증",
    "이벤트 참여 인증",
    "구매 인증",
]

# =========================================================
# 데이터
# =========================================================
def empty_data():
    return {
        "threads": {key: None for key in THREAD_KEYS},
        "requests": {},
        "coins": {},
        "used_invite_members": {},
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        return empty_data()

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return empty_data()

        data.setdefault("threads", {})
        data.setdefault("requests", {})
        data.setdefault("coins", {})
        data.setdefault("used_invite_members", {})

        if not isinstance(data["threads"], dict):
            data["threads"] = {}

        if not isinstance(data["requests"], dict):
            data["requests"] = {}
        if not isinstance(data["coins"], dict):
            data["coins"] = {}
        if not isinstance(data["used_invite_members"], dict):
            data["used_invite_members"] = {}

        for key in THREAD_KEYS:
            data["threads"].setdefault(key, None)

        return data

    except Exception as e:
        print(f"[WARN] 데이터 로드 실패: {e}")
        return empty_data()


data = load_data()


def save_data():
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)

    temp_file = DATA_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.replace(temp_file, DATA_FILE)


REWARDS = {
    "추천 인증": 1,
    "후기 작성 인증": 1,
    "초대 인증": 2,
    "이벤트 참여 인증": 1,
    "구매 인증": 0,
}


def add_coins(user_id, amount):
    if amount <= 0:
        return 0

    key = str(user_id)
    data["coins"][key] = int(data["coins"].get(key, 0)) + amount
    return data["coins"][key]


def now_kst():
    return (
        datetime.now(timezone.utc) + timedelta(hours=9)
    ).strftime("%Y-%m-%d %H:%M:%S")


def make_request_id():
    return datetime.now().strftime("%Y%m%d%H%M%S%f")


def is_admin(user):
    return user.id == OWNER_ID or (
        isinstance(user, discord.Member)
        and (
            user.guild_permissions.administrator
            or user.guild_permissions.manage_guild
        )
    )


async def get_configured_thread(guild, key):
    thread_id = data["threads"].get(key)

    if not thread_id:
        return None

    return guild.get_channel_or_thread(int(thread_id))


# =========================================================
# Embed
# =========================================================
def make_request_embed(
    request_id,
    request_type,
    user,
    fields,
    status="⏳ 승인 대기",
    image_url=None,
):
    embed = discord.Embed(
        title=f"﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒\n{request_type}",
        description=f"👤 신청자: {user.mention}",
        color=discord.Color.from_rgb(184, 163, 255),
    )

    for name, value in fields:
        embed.add_field(
            name=name,
            value=str(value),
            inline=False,
        )

    embed.add_field(
        name="상태",
        value=status,
        inline=False,
    )

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text=f"신청 ID: {request_id}")

    return embed


async def send_request_to_thread(
    interaction,
    request_id,
    request_type,
    embed,
    files=None,
    extra_embeds=None,
):
    # 인증 신청 자체는 항상 관리자용으로 설정한 인증 스레드에 보냅니다.
    # 단, 참여자가 인증을 제출한 곳이 자기 스레드라면 그 스레드 ID를
    # request 데이터에 별도로 저장해서 승인/반려 결과만 그곳으로 보냅니다.
    thread = await get_configured_thread(
        interaction.guild,
        request_type,
    )

    if not isinstance(thread, discord.Thread):
        await interaction.followup.send(
            f"⚠️ **{request_type}** 스레드가 아직 설정되지 않았어요.\n"
            f"관리자가 `/인증설정`으로 연결해주세요.",
            ephemeral=True,
        )
        return None

    embeds = [embed]
    if extra_embeds:
        embeds.extend(extra_embeds)

    message = await thread.send(
        embeds=embeds,
        files=files or [],
        view=AdminRequestView(),
    )

    return message


def get_result_thread(guild, request):
    # 참여자가 자기 스레드에서 인증을 제출한 경우
    # 승인/반려 결과를 그 스레드로 보냅니다.
    source_thread_id = request.get("source_thread_id")
    if not source_thread_id:
        return None

    channel = guild.get_channel_or_thread(int(source_thread_id))
    return channel if isinstance(channel, discord.Thread) else None


async def send_result_to_participant_thread(guild, request, embed, files=None):
    thread = get_result_thread(guild, request)
    if not thread:
        return None

    try:
        return await thread.send(
            embed=embed,
            files=files or [],
        )
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
        print(f"[WARN] 참여자 스레드 결과 전송 실패: {e}")
        return None


# =========================================================
# 사진 2장을 하나의 임베드 이미지로 나란히 합치기
# =========================================================
async def make_photo_collage(attachments):
    if len(attachments) == 1:
        try:
            # attachment:// URL과 실제 업로드 파일명이 반드시 같아야
            # 이미지가 임베드 안에서 렌더링됩니다.
            file = await attachments[0].to_file()
            original_name = getattr(file, "filename", "image.png") or "image.png"
            ext = os.path.splitext(original_name)[1] or ".png"
            filename = f"verification_{attachments[0].id}{ext}"
            file.filename = filename
            return file, filename
        except Exception as e:
            print(f"[ERROR] 이미지 파일 변환 실패: {e}")
            return None, None

    try:
        images = []
        async with aiohttp.ClientSession() as session:
            for attachment in attachments[:2]:
                async with session.get(attachment.url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"이미지 다운로드 실패: HTTP {resp.status}")
                    raw = await resp.read()
                image = Image.open(io.BytesIO(raw)).convert("RGB")
                images.append(image)

        target_height = 700
        resized = []
        for image in images:
            ratio = target_height / image.height
            width = max(1, int(image.width * ratio))
            resized.append(image.resize((width, target_height), Image.Resampling.LANCZOS))

        gap = 16
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
        filename = "verification_photos.png"
        return discord.File(output, filename=filename), filename
    except Exception as e:
        print(f"[ERROR] 사진 합치기 실패: {e}")
        return None, None


# =========================================================
# 사진 직접 첨부 모달
# Discord / discord.py 2.7+
# =========================================================
class PhotoVerificationModal(discord.ui.Modal):
    def __init__(self, request_type):
        super().__init__(title=request_type)

        self.request_type = request_type

        self.file_upload = discord.ui.FileUpload(
            custom_id=f"verification:{request_type}:file",
            min_values=1,
            max_values=2,
            required=True,
        )

        self.add_item(
            discord.ui.Label(
                text="인증 사진",
                description="인증에 필요한 사진을 최대 2장까지 첨부해주세요.",
                component=self.file_upload,
            )
        )

    async def on_submit(self, interaction):
        attachments = list(self.file_upload.values or [])

        if not attachments:
            await interaction.response.send_message(
                "❌ 인증 사진을 1장 이상 첨부해주세요.",
                ephemeral=True,
            )
            return

        if len(attachments) > 2:
            await interaction.response.send_message(
                "❌ 인증 사진은 최대 2장까지 첨부할 수 있어요.",
                ephemeral=True,
            )
            return

        for attachment in attachments:
            content_type = attachment.content_type or ""
            if not content_type.startswith("image/"):
                await interaction.response.send_message(
                    "❌ 인증 사진은 이미지 파일만 첨부할 수 있어요.",
                    ephemeral=True,
                )
                return

        collage_file, collage_filename = await make_photo_collage(attachments)
        if collage_file is None:
            await interaction.response.send_message(
                "❌ 사진을 처리하는 중 오류가 발생했어요. 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        request_id = make_request_id()

        data["requests"][request_id] = {
            "type": self.request_type,
            "user_id": interaction.user.id,
            "status": "pending",
            "created_at": now_kst(),
            "filenames": [a.filename for a in attachments],
            "attachment_urls": [a.url for a in attachments],
            "source_thread_id": (
                interaction.channel.id
                if isinstance(interaction.channel, discord.Thread)
                else None
            ),
        }

        await interaction.response.defer(ephemeral=True)

        embed = make_request_embed(
            request_id,
            self.request_type,
            interaction.user,
            [
                ("📷 인증 사진", "임베드에서 인증 사진을 확인해주세요."),
            ],
            image_url=f"attachment://{collage_filename}",
        )

        # 두 사진은 하나의 이미지로 합쳐 임베드 안에 나란히 표시합니다.
        message = await send_request_to_thread(
            interaction,
            request_id,
            self.request_type,
            embed,
            [collage_file],
        )

        if message is None:
            data["requests"].pop(request_id, None)
            save_data()
            return

        data["requests"][request_id]["message_id"] = message.id
        data["requests"][request_id]["thread_id"] = message.channel.id
        save_data()

        await interaction.followup.send(
            "✅ 인증 사진이 정상적으로 접수됐어요!\n"
            "관리자 확인 후 결과를 알려드릴게요.",
            ephemeral=True,
        )


# =========================================================
# 초대 / 이벤트 참여용 일반 입력 모달
# 사용자가 구체적인 인증 방식은 아직 정하지 않았으므로
# 일단 내용 입력 + 선택 사진으로 구성
# =========================================================
class TextVerificationModal(discord.ui.Modal):
    def __init__(self, request_type):
        super().__init__(title=request_type)

        self.request_type = request_type

        self.content = discord.ui.TextInput(
            label="인증 내용",
            placeholder="인증에 필요한 내용을 작성해주세요.",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
        )

        self.add_item(self.content)

    async def on_submit(self, interaction):
        request_id = make_request_id()

        data["requests"][request_id] = {
            "type": self.request_type,
            "user_id": interaction.user.id,
            "status": "pending",
            "created_at": now_kst(),
            "content": self.content.value,
            "source_thread_id": (
                interaction.channel.id
                if isinstance(interaction.channel, discord.Thread)
                else None
            ),
        }

        await interaction.response.defer(ephemeral=True)

        embed = make_request_embed(
            request_id,
            self.request_type,
            interaction.user,
            [
                ("📝 인증 내용", self.content.value),
            ],
        )

        message = await send_request_to_thread(
            interaction,
            request_id,
            self.request_type,
            embed,
        )

        if message is None:
            data["requests"].pop(request_id, None)
            save_data()
            return

        data["requests"][request_id]["message_id"] = message.id
        data["requests"][request_id]["thread_id"] = message.channel.id
        save_data()

        await interaction.followup.send(
            "✅ 인증 신청이 접수됐어요!",
            ephemeral=True,
        )


# =========================================================
# 초대 인증
# /초대인증 @새로들어온사람
# =========================================================
class InviteVerificationView(discord.ui.View):
    def __init__(self, inviter_id, invited_member_id):
        super().__init__(timeout=86400)
        self.inviter_id = inviter_id
        self.invited_member_id = invited_member_id

    @discord.ui.button(
        label="제출하기",
        emoji="📨",
        style=discord.ButtonStyle.success,
        custom_id="verification:invite:submit_once",
    )
    async def submit(self, interaction, button):
        if interaction.user.id != self.inviter_id:
            await interaction.response.send_message(
                "❌ 이 초대 인증을 만든 사람만 제출할 수 있어요.",
                ephemeral=True,
            )
            return

        invited = interaction.guild.get_member(self.invited_member_id)
        if invited is None:
            await interaction.response.send_message(
                "❌ 멘션한 멤버가 서버에 없습니다.",
                ephemeral=True,
            )
            return

        invited_key = str(invited.id)
        if invited_key in data["used_invite_members"]:
            await interaction.response.send_message(
                "⚠️ 해당 멤버는 이미 초대 인증에 사용됐어요.",
                ephemeral=True,
            )
            return

        thread = await get_configured_thread(interaction.guild, "초대 인증")
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                "⚠️ **초대 인증** 스레드가 아직 설정되지 않았어요.\n관리자가 `/인증설정`으로 연결해주세요.",
                ephemeral=True,
            )
            return

        request_id = make_request_id()
        reward = REWARDS["초대 인증"]
        new_balance = add_coins(interaction.user.id, reward)

        data["used_invite_members"][invited_key] = {
            "inviter_id": interaction.user.id,
            "request_id": request_id,
            "created_at": now_kst(),
        }
        data["requests"][request_id] = {
            "type": "초대 인증",
            "user_id": interaction.user.id,
            "invited_member_id": invited.id,
            "status": "approved",
            "created_at": now_kst(),
            "processed_at": now_kst(),
            "coins_awarded": reward,
        }

        embed = make_request_embed(
            request_id,
            "초대 인증",
            interaction.user,
            [
                ("👤 초대받은 멤버", invited.mention),
                ("🪙 지급 코인", f"+{reward} 코인"),
                ("💰 현재 코인", f"{new_balance} 코인"),
            ],
            status="✅ 자동 인증 완료",
        )

        await interaction.response.defer(ephemeral=True)
        message = await send_request_to_thread(
            interaction,
            request_id,
            "초대 인증",
            embed,
        )

        if message is not None:
            data["requests"][request_id]["message_id"] = message.id
            data["requests"][request_id]["thread_id"] = message.channel.id

        save_data()

        await interaction.followup.send(
            f"✅ 초대 인증이 완료됐어요! **+{reward} 코인** 지급됐습니다.\n현재 보유 코인: **{new_balance}개**",
            ephemeral=True,
        )


# =========================================================
# 구매 인증
# /직접구매
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
            placeholder="예: 프로필 카드",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
        )

        self.add_item(self.coins)
        self.add_item(self.product)

    async def on_submit(self, interaction):
        coins = self.coins.value.strip()
        product = self.product.value.strip()

        request_id = make_request_id()

        data["requests"][request_id] = {
            "type": "구매 인증",
            "user_id": interaction.user.id,
            "status": "pending",
            "created_at": now_kst(),
            "coins": coins,
            "product": product,
            "source_thread_id": (
                interaction.channel.id
                if isinstance(interaction.channel, discord.Thread)
                else None
            ),
        }

        await interaction.response.defer(ephemeral=True)

        embed = make_request_embed(
            request_id,
            "구매 인증",
            interaction.user,
            [
                ("🪙 코인 갯수", coins),
                ("🎨 원하는 제작물", product),
            ],
        )

        message = await send_request_to_thread(
            interaction,
            request_id,
            "구매 인증",
            embed,
        )

        if message is None:
            data["requests"].pop(request_id, None)
            save_data()
            return

        data["requests"][request_id]["message_id"] = message.id
        data["requests"][request_id]["thread_id"] = message.channel.id
        save_data()

        await interaction.followup.send(
            "✅ 구매 신청이 접수됐어요!\n"
            "관리자 확인 후 승인 또는 반려됩니다.",
            ephemeral=True,
        )


# =========================================================
# 반려 사유
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
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ 관리자만 처리할 수 있습니다.",
                ephemeral=True,
            )
            return

        request = data["requests"].get(self.request_id)

        if not request:
            await interaction.response.send_message(
                "❌ 신청 정보를 찾을 수 없습니다.",
                ephemeral=True,
            )
            return

        if request.get("status") != "pending":
            await interaction.response.send_message(
                "⚠️ 이미 처리된 신청입니다.",
                ephemeral=True,
            )
            return

        reason = self.reason.value.strip()

        request["status"] = "rejected"
        request["reason"] = reason
        request["processed_at"] = now_kst()
        request["processed_by"] = interaction.user.id

        save_data()

        embed = interaction.message.embeds[0]

        embed.color = discord.Color.red()

        for index, field in enumerate(embed.fields):
            if field.name == "상태":
                embed.set_field_at(
                    index,
                    name="상태",
                    value="❌ 반려",
                    inline=False,
                )
                break

        embed.add_field(
            name="❌ 반려 사유",
            value=reason,
            inline=False,
        )

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )

        # 신청자가 본인 스레드에서 제출했다면 반려 결과만 그 스레드로 보냅니다.
        result_embed = discord.Embed(
            title=f"﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒\n{request['type']}",
            description=f"**{request['type']}** 인증이 반려되었습니다.\n\n❌ **반려 사유**\n{reason}",
            color=discord.Color.red(),
        )
        result_embed.set_footer(text=f"신청 ID: {self.request_id}")
        await send_result_to_participant_thread(
            interaction.guild,
            request,
            result_embed,
        )

        user = interaction.guild.get_member(
            int(request["user_id"])
        )

        if user:
            try:
                await user.send(
                    f"❌ **{request['type']}** 인증이 반려됐어요.\n"
                    f"반려 사유: {reason}"
                )
            except discord.Forbidden:
                pass


# =========================================================
# 관리자 승인 / 반려
# =========================================================
class AdminRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def get_request_id(self, interaction):
        if not interaction.message.embeds:
            return None

        footer = interaction.message.embeds[0].footer.text or ""

        if footer.startswith("신청 ID: "):
            return footer.replace("신청 ID: ", "", 1).strip()

        return None

    @discord.ui.button(
        label="승인",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="verification:admin:approve",
    )
    async def approve(self, interaction, button):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ 관리자만 처리할 수 있습니다.",
                ephemeral=True,
            )
            return

        request_id = self.get_request_id(interaction)
        request = data["requests"].get(request_id)

        if not request or request.get("status") != "pending":
            await interaction.response.send_message(
                "⚠️ 이미 처리됐거나 존재하지 않는 신청입니다.",
                ephemeral=True,
            )
            return

        request["status"] = "approved"
        request["processed_at"] = now_kst()
        request["processed_by"] = interaction.user.id

        reward = REWARDS.get(request.get("type"), 0)
        if reward > 0 and not request.get("coins_awarded"):
            new_balance = add_coins(request["user_id"], reward)
            request["coins_awarded"] = reward
            request["coin_balance"] = new_balance
        else:
            new_balance = data["coins"].get(str(request["user_id"]), 0)

        save_data()

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()

        for index, field in enumerate(embed.fields):
            if field.name == "상태":
                embed.set_field_at(
                    index,
                    name="상태",
                    value="✅ 승인",
                    inline=False,
                )
                break

        if reward > 0:
            embed.add_field(
                name="🪙 코인 지급",
                value=f"+{reward} 코인 · 현재 {new_balance} 코인",
                inline=False,
            )

        await interaction.response.edit_message(
            embed=embed,
            view=None,
        )

        # 참여자 본인 스레드에는 승인 결과만 간단하게 보냅니다.
        result_embed = discord.Embed(
            description=(
                f"**{request['type']}** 인증이 승인 완료되었습니다.\n"
                "🪙 코인 지급\n"
                f"+{reward} 코인 · 현재 {new_balance} 코인"
            ),
            color=discord.Color.green(),
        )

        await send_result_to_participant_thread(
            interaction.guild,
            request,
            result_embed,
        )

        user = interaction.guild.get_member(
            int(request["user_id"])
        )

        if user:
            try:
                if reward > 0:
                    await user.send(
                        f"✅ **{request['type']}** 인증이 승인됐어요!\n"
                        f"🪙 **+{reward} 코인** 지급됐습니다.\n"
                        f"💰 현재 보유 코인: **{new_balance}개**"
                    )
                else:
                    await user.send(
                        f"✅ **{request['type']}** 인증이 승인됐어요!"
                    )
            except discord.Forbidden:
                pass

    @discord.ui.button(
        label="반려",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="verification:admin:reject",
    )
    async def reject(self, interaction, button):
        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "❌ 관리자만 처리할 수 있습니다.",
                ephemeral=True,
            )
            return

        request_id = self.get_request_id(interaction)

        if not request_id or request_id not in data["requests"]:
            await interaction.response.send_message(
                "❌ 신청 정보를 찾을 수 없습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            RejectModal(request_id)
        )


# =========================================================
# 사용자 인증 패널
# =========================================================
class MainVerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="추천 인증",
        emoji="📸",
        style=discord.ButtonStyle.primary,
        custom_id="verification:user:recommend",
        row=0,
    )
    async def recommend(self, interaction, button):
        await interaction.response.send_modal(
            PhotoVerificationModal("추천 인증")
        )

    @discord.ui.button(
        label="후기 작성 인증",
        emoji="📝",
        style=discord.ButtonStyle.primary,
        custom_id="verification:user:review",
        row=0,
    )
    async def review(self, interaction, button):
        await interaction.response.send_modal(
            PhotoVerificationModal("후기 작성 인증")
        )

    @discord.ui.button(
        label="이벤트 참여 인증",
        emoji="🎉",
        style=discord.ButtonStyle.secondary,
        custom_id="verification:user:event",
        row=1,
    )
    async def event(self, interaction, button):
        await interaction.response.send_modal(
            PhotoVerificationModal("이벤트 참여 인증")
        )

    @discord.ui.button(
        label="구매 인증",
        emoji="🛒",
        style=discord.ButtonStyle.success,
        custom_id="verification:user:purchase",
        row=2,
    )
    async def purchase(self, interaction, button):
        await interaction.response.send_modal(
            PurchaseModal()
        )


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
        # 재시작 후에도 기존 버튼 작동
        self.add_view(MainVerificationView())
        self.add_view(AdminRequestView())

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(
                f"[INFO] 서버 명령어 동기화 완료: {len(synced)}개"
            )
        else:
            synced = await self.tree.sync()
            print(
                f"[INFO] 전역 명령어 동기화 완료: {len(synced)}개"
            )


bot = VerificationBot()


# =========================================================
# /인증패널
# =========================================================
@bot.tree.command(
    name="인증패널",
    description="인증 제출 패널을 생성합니다.",
)
async def verification_panel(interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "❌ 관리자만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒\n4ever 인증 접수",
        description=(
            "아래에서 해당하는 인증을 선택해주세요.\n\n"
            "📸 **추천 인증** — 사진 필수\n"
            "📝 **후기 작성 인증** — 사진 필수\n"
            "👥 **초대 인증** — `/초대인증` 명령어 사용\n"
            "🎉 **이벤트 참여 인증** — 사진 필수\n"
            "🛒 **구매 인증** — 코인 / 원하는 제작물 입력"
        ),
        color=discord.Color.from_rgb(184, 163, 255),
    )

    await interaction.response.send_message(
        embed=embed,
        view=MainVerificationView(),
    )


# =========================================================
# /초대인증
# =========================================================
@bot.tree.command(
    name="초대인증",
    description="새로 들어온 멤버를 멘션해 초대 인증을 제출합니다.",
)
@app_commands.describe(
    멤버="초대를 통해 새로 들어온 멤버",
)
async def invite_verification(interaction, 멤버: discord.Member):
    if 멤버.id == interaction.user.id:
        await interaction.response.send_message(
            "❌ 본인을 초대 멤버로 등록할 수 없습니다.",
            ephemeral=True,
        )
        return

    if str(멤버.id) in data["used_invite_members"]:
        await interaction.response.send_message(
            "⚠️ 해당 멤버는 이미 초대 인증에 사용됐어요.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="﹒︶︶﹒︶︶୨୧︶︶﹒︶︶﹒\n초대 인증",
        description=(
            f"👤 초대자: {interaction.user.mention}\n"
            f"✨ 초대받은 멤버: {멤버.mention}\n\n"
            "초대받은 멤버가 맞는지 확인한 뒤 **제출하기**를 눌러주세요.\n"
            "승인 시 **+2 코인**이 자동 지급됩니다."
        ),
        color=discord.Color.from_rgb(184, 163, 255),
    )

    await interaction.response.send_message(
        embed=embed,
        view=InviteVerificationView(interaction.user.id, 멤버.id),
    )


# =========================================================
# /내코인
# =========================================================
@bot.tree.command(
    name="코인",
    description="현재 보유한 4ever 코인을 확인합니다.",
)
async def my_coins(interaction):
    balance = int(data["coins"].get(str(interaction.user.id), 0))
    await interaction.response.send_message(
        f"🪙 {interaction.user.mention}님의 현재 코인은 **{balance}개**예요!",
        ephemeral=True,
    )


# =========================================================
# /직접구매
# =========================================================
@bot.tree.command(
    name="직접구매",
    description="원하는 제작물을 직접 구매 신청합니다.",
)
async def direct_purchase(interaction):
    await interaction.response.send_modal(
        PurchaseModal()
    )


# =========================================================
# /인증설정
# =========================================================
@bot.tree.command(
    name="인증설정",
    description="인증 종류와 미리 만들어둔 스레드를 연결합니다.",
)
@app_commands.describe(
    종류="인증 종류",
    스레드="해당 인증이 들어갈 스레드",
)
@app_commands.choices(
    종류=[
        app_commands.Choice(name=key, value=key)
        for key in THREAD_KEYS
    ]
)
async def set_thread(
    interaction,
    종류: app_commands.Choice[str],
    스레드: discord.Thread,
):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(
            "❌ 봇 소유자만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    data["threads"][종류.value] = str(스레드.id)
    save_data()

    await interaction.response.send_message(
        f"✅ **{종류.value}** → {스레드.mention}\n"
        "설정 완료!",
        ephemeral=True,
    )


# =========================================================
# /인증설정확인
# =========================================================
@bot.tree.command(
    name="인증설정확인",
    description="현재 인증 스레드 연결 상태를 확인합니다.",
)
async def show_thread_settings(interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "❌ 관리자만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    lines = []

    for key in THREAD_KEYS:
        thread_id = data["threads"].get(key)

        if not thread_id:
            lines.append(f"• **{key}** → ❌ 미설정")
            continue

        thread = interaction.guild.get_channel_or_thread(
            int(thread_id)
        )

        if thread:
            lines.append(
                f"• **{key}** → {thread.mention}"
            )
        else:
            lines.append(
                f"• **{key}** → ⚠️ 스레드를 찾을 수 없음"
            )

    await interaction.response.send_message(
        "\n".join(lines),
        ephemeral=True,
    )


# =========================================================
# 시작
# =========================================================
@bot.event
async def on_ready():
    print(f"[INFO] 로그인 완료: {bot.user}")


bot.run(TOKEN)
