import asyncio
import discord
import yt_dlp as youtube_dl
from discord.ext import commands
from dico_token import Token

youtube_dl.utils.bug_reports_message = lambda: ''

ytdl_format_options = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}
ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}
ytdl = youtube_dl.YoutubeDL(ytdl_format_options)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url')

    @classmethod
    async def from_query(cls, query, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=not stream))
        if 'entries' in data:
            data = data['entries'][0]
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.autoplay_enabled = False
        self.current_ctx: commands.Context | None = None
        self.current_url: str | None = None
        self.current_song_title: str | None = None
        self.is_playing = False  # 현재 곡 재생 상태

    async def play_song(self, ctx, query):
        """query(링크/검색어)로 곡을 재생하고 after 콜백에서 자동재생을 이어감"""
        player = await YTDLSource.from_query(query, loop=self.bot.loop, stream=True)
        vc = ctx.voice_client

        # 기존 재생 중이면 정지 후 짧게 대기
        if vc and vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.5)

        # 현재 곡 메타데이터 저장
        self.current_url = player.url
        self.current_ctx = ctx
        self.current_song_title = player.title or ''
        self.is_playing = True

        def after_playing(err):
            self.is_playing = False
            if err:
                print(f"[Error] {err}")
            if self.autoplay_enabled:
                asyncio.run_coroutine_threadsafe(self.play_next_song(), self.bot.loop)

        vc.play(player, after=after_playing)
        await ctx.send(f'Now playing: {player.title}')

    def _pick_next_from_related(self, info_dict: dict) -> str | None:
        """
        yt_dlp가 제공하는 related_videos(유튜브의 '다음 추천/관련 영상')에서
        현재 곡과 다른 첫 번째 후보를 선택하여 URL을 반환.
        """
        related = info_dict.get('related_videos') or []
        if not related:
            print("[Autoplay] related_videos가 비어 있습니다.")
            return None

        # 현재 영상의 ID 추출
        def get_video_id(url: str) -> str | None:
            # 매우 단순한 추출기 (youtube watch?v=ID 형태 가정)
            import urllib.parse as up
            try:
                qs = up.urlparse(url)
                if qs.netloc.endswith("youtube.com"):
                    q = up.parse_qs(qs.query)
                    return (q.get("v") or [None])[0]
                if qs.netloc.endswith("youtu.be"):
                    # /ID 형태
                    return qs.path.lstrip("/")
            except Exception:
                return None
            return None

        current_id = get_video_id(self.current_url or "")

        for cand in related:
            # yt_dlp는 각 후보에 id/title 등이 포함됨
            vid = cand.get('id')
            if not vid:
                continue
            if current_id and vid == current_id:
                continue
            # 유튜브 URL 구성
            return f"https://www.youtube.com/watch?v={vid}"

        return None

    async def play_next_song(self, *, force: bool = False):
        """
        현재 곡의 'related_videos'(Up next/관련 영상)에서 다음 곡을 선택해 재생.
        - force=True 이면 autoplay 상태와 무관하게 동작(수동 스킵 지원)
        """
        if not force and not self.autoplay_enabled:
            return
        # (콜백 경합 방지) 이미 재생 시작 중이면 종료
        if self.is_playing:
            return
        if not self.current_url or not self.current_ctx:
            print("다음 곡을 재생할 컨텍스트/URL이 없습니다.")
            return

        try:
            # 현재 곡의 전체 정보에서 추천목록 가져오기
            current_info = ytdl.extract_info(self.current_url, download=False)
            next_url = self._pick_next_from_related(current_info)

            if not next_url:
                print("[Autoplay] 추천 영상을 찾지 못해 현재 곡을 다시 재생합니다.")
                await self.current_ctx.send("추천 영상을 찾지 못해 현재 곡을 다시 재생합니다. 🔂")
                next_url = self.current_url

            await self.play_song(self.current_ctx, next_url)

        except Exception as e:
            print(f"[Autoplay] 다음 곡 재생 오류: {e}")
            import traceback
            traceback.print_exc()

    @commands.command()
    async def join(self, ctx):
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            await channel.connect()
        else:
            await ctx.send("음성 채널에 먼저 접속해주세요.")

    @commands.command()
    async def play(self, ctx, *, query):
        """(원래 코드와 호환) play도 자동재생을 켭니다."""
        self.autoplay_enabled = True
        await self.ensure_voice(ctx)
        await self.play_song(ctx, query)

    @commands.command()
    async def autoplay(self, ctx, *, query):
        """
        자동재생 모드 시작: 첫 곡만 재생하고
        이후는 after 콜백 → play_next_song()이 이어서 처리
        """
        self.autoplay_enabled = True
        await self.ensure_voice(ctx)
        await self.play_song(ctx, query)
        await ctx.send("🎶 자동 재생 모드가 켜졌습니다!")

    @commands.command(aliases=["skip", "next"])
    async def nextsong(self, ctx):
        """
        ⏭ 다음 곡으로 즉시 넘기기.
        - 자동재생이 켜져 있으면 stop() → after 콜백이 추천 기반 다음 곡 재생
        - 자동재생이 꺼져 있어도 강제로 추천을 찾아 재생 (force=True)
        """
        if not ctx.voice_client:
            await ctx.send("먼저 음성 채널에 들어가서 곡을 재생해 주세요.")
            return

        await ctx.send("⏭ 다음 곡으로 넘어갑니다.")
        was_autoplay = self.autoplay_enabled

        # 재생 중이면 즉시 정지 (after 콜백이 트리거됨)
        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()

        # 자동재생이 꺼져 있으면 추천 기반으로 수동 탐색
        if not was_autoplay:
            await self.play_next_song(force=True)

    @commands.command()
    async def stopautoplay(self, ctx):
        self.autoplay_enabled = False
        await ctx.send("🔇 자동 재생이 중단되었습니다.")

    @commands.command()
    async def stop(self, ctx):
        self.autoplay_enabled = False
        if ctx.voice_client:
            await ctx.voice_client.disconnect()

    @commands.command()
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸ 음악 일시 정지")

    @commands.command()
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ 음악 다시 재생")

    @commands.command()
    async def volume(self, ctx, volume: int):
        if ctx.voice_client and ctx.voice_client.source:
            ctx.voice_client.source.volume = volume / 100
            await ctx.send(f"🔊 볼륨: {volume}%")

    async def ensure_voice(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("음성 채널에 먼저 접속해주세요.")
                raise commands.CommandError("Author not connected to a voice channel.")
        elif ctx.voice_client.is_playing():
            ctx.voice_client.stop()


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')


async def main():
    async with bot:
        await bot.add_cog(Music(bot))
        await bot.start(Token)

asyncio.run(main())
