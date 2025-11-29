import asyncio
import os
import discord
from discord.ext import commands
from pytubefix import YouTube, Search, Playlist

# FFmpeg 옵션
ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

class PyTubeSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url')

    @classmethod
    async def from_query(cls, query, *, loop=None):
        loop = loop or asyncio.get_event_loop()

        def get_info(q):
            try:
                # OAuth 및 PO_TOKEN 설정
                kwargs = {
                    'use_oauth': True,
                    'allow_oauth_cache': True
                }
                po_token = os.getenv('PO_TOKEN')
                if po_token:
                    kwargs['po_token'] = po_token

                if q.startswith('http'):
                    yt = YouTube(q, **kwargs)
                else:
                    s = Search(q, client='WEB')
                    if not s.results:
                        raise Exception("검색 결과가 없습니다.")
                    # 검색 결과의 URL로 YouTube 객체 생성 (OAuth 적용)
                    yt = YouTube(s.results[0].watch_url, **kwargs)
                
                audio_stream = yt.streams.filter(only_audio=True).order_by('abr').desc().first()
                if not audio_stream:
                    raise Exception("오디오 스트림을 찾을 수 없습니다.")

                return {
                    'title': yt.title,
                    'url': audio_stream.url,
                    'webpage_url': yt.watch_url,
                    'duration': yt.length,
                    'video_id': yt.video_id
                }
            except Exception as e:
                print(f"Error extracting info: {e}")
                raise e

        data = await loop.run_in_executor(None, lambda: get_info(query))
        filename = data['url']
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.current_ctx = None
        self.autoplay = True
        self.current_video_id = None
        self.current_title = None
        self.queue = [] # 노래 대기열

    @commands.command()
    async def join(self, ctx):
        if ctx.author.voice:
            channel = ctx.author.voice.channel
            if ctx.voice_client is not None:
                return await ctx.voice_client.move_to(channel)
            await channel.connect()
        else:
            await ctx.send("음성 채널에 먼저 입장해주세요.")

    @commands.command()
    async def play(self, ctx, *, query):
        """URL이나 검색어로 음악을 재생합니다. 플레이리스트 URL도 지원합니다."""
        if not ctx.voice_client:
            await self.join(ctx)
            if not ctx.voice_client:
                return

        self.current_ctx = ctx

        # 플레이리스트 처리
        if 'list=' in query and 'http' in query and 'watch?v=' not in query:
            # music.youtube.com 호환성 처리
            query = query.replace('music.youtube.com', 'www.youtube.com')
            
            await ctx.send(f"플레이리스트 정보를 분석 중입니다...")

            def get_playlist_info(url):
                try:
                    # OAuth 및 PO_TOKEN 설정
                    kwargs = {
                        'use_oauth': True,
                        'allow_oauth_cache': True
                    }
                    po_token = os.getenv('PO_TOKEN')
                    if po_token:
                        kwargs['po_token'] = po_token

                    p = Playlist(url, client='WEB', **kwargs)
                    # title 접근이나 video_urls 접근 시 네트워크 요청 발생 가능
                    title = p.title
                    # video_urls는 전체를 가져올 수 있으므로 주의. 
                    # executor에서 실행하므로 봇은 멈추지 않음.
                    urls = []
                    for video_url in p.video_urls:
                        urls.append(video_url)
                        if len(urls) >= 30:
                            break
                    return title, urls
                except Exception as e:
                    print(f"Playlist error: {e}")
                    return None, []

            title, urls = await self.bot.loop.run_in_executor(None, lambda: get_playlist_info(query))

            if not title and not urls:
                 await ctx.send("플레이리스트를 불러오는 중 오류가 발생했습니다.")
                 return

            if title:
                await ctx.send(f"플레이리스트 **{title}**에서 곡을 불러왔습니다.")
            
            count = 0
            for url in urls:
                self.queue.append(url)
                count += 1
            
            await ctx.send(f"총 {count}곡이 대기열에 추가되었습니다. (최대 30곡)")
            
            if not ctx.voice_client.is_playing():
                await self.play_next_in_queue()
            return

        # 일반 단일 곡 처리
        self.queue.append(query)
        if not ctx.voice_client.is_playing():
            await self.play_next_in_queue()
        else:
            await ctx.send(f"대기열에 추가됨: {query}")

    async def play_next_in_queue(self):
        if not self.queue:
            # 대기열이 비었으면 자동재생 시도
            if self.autoplay and self.current_video_id:
                 await self.play_autoplay()
            return

        query = self.queue.pop(0)
        
        try:
            player = await PyTubeSource.from_query(query, loop=self.bot.loop)
            
            if self.current_ctx.voice_client.is_playing():
                self.current_ctx.voice_client.stop()
            
            self.current_title = player.title
            self.current_video_id = player.data.get('video_id')

            self.current_ctx.voice_client.play(player, after=self.after_playing)
            await self.current_ctx.send(f'Now playing: **{player.title}**')
        except Exception as e:
            await self.current_ctx.send(f"재생 오류 ({query}): {e}")
            # 오류 발생 시 다음 곡 시도
            await self.play_next_in_queue()

    def after_playing(self, error):
        if error:
            print(f'Player error: {error}')
        
        # 다음 곡 재생 (대기열 -> 자동재생 순)
        asyncio.run_coroutine_threadsafe(self.play_next_in_queue(), self.bot.loop)

    async def play_autoplay(self):
        print("Autoplay triggered")
        next_url = await self.get_recommendation(self.current_title, self.current_video_id)
        if next_url:
            print(f"Autoplay URL: {next_url}")
            # 자동재생 곡을 대기열 맨 앞에 추가하고 재생
            self.queue.insert(0, next_url)
            await self.play_next_in_queue()
        else:
            await self.current_ctx.send("자동재생 곡을 찾을 수 없습니다.")

    async def get_recommendation(self, title, current_video_id):
        def _search():
            try:
                search_query = title
                s = Search(search_query)
                if not s.results:
                    import time
                    time.sleep(1)
                    s = Search(search_query)
                
                if not s.results:
                    return None

                for vid in s.results:
                    if vid.video_id != current_video_id:
                        return vid.watch_url
                return None
            except Exception as e:
                print(f"Recommendation error: {e}")
                return None
        
        return await self.bot.loop.run_in_executor(None, _search)

    @commands.command()
    async def stop(self, ctx):
        self.queue.clear() # 대기열 초기화
        if ctx.voice_client:
            ctx.voice_client.stop()
            await ctx.voice_client.disconnect()
            await ctx.send("재생을 멈추고 채널을 떠났습니다.")

    @commands.command()
    async def skip(self, ctx):
        """현재 곡을 스킵합니다."""
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("스킵됨.")

    @commands.command()
    async def volume(self, ctx, volume: int):
        if ctx.voice_client is None:
            return await ctx.send("음성 채널에 연결되어 있지 않습니다.")
        ctx.voice_client.source.volume = volume / 100
        await ctx.send(f"볼륨을 {volume}%로 설정했습니다.")

    @play.before_invoke
    async def ensure_voice(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("음성 채널에 연결되어 있지 않습니다.")
                raise commands.CommandError("Author not connected to a voice channel.")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    await bot.add_cog(Music(bot))

bot.run(os.getenv('DISCORD_TOKEN'))
