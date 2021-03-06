import datetime as dt
import logging
import os
from threading import Thread, Event
import time

import discord
from discord.ext import commands
import pytz

if not discord.opus.is_loaded():
    discord.opus.load_opus('./libopus.so')

logging.basicConfig(level=logging.INFO)


def is_dst(zonename):
    """
    Check if a pytz timezone is currently in DST.

    :param zonename: pytz timezone name
    :type zonename: string
    :return: true if the timezone is in DST
    :rtype: boolean
    """
    tz = pytz.timezone(zonename)
    now = pytz.utc.localize(dt.datetime.utcnow())
    return now.astimezone(tz).dst() != dt.timedelta(0)


def possible_timezones(tz_offset, common_only=True):
    """
    Get a list of possible timezones for a UTC offset.

    :param tz_offset: the UTC offset in hours
    :type tz_offset: int
    :param common_only: only use commonly used timezones (default True)
    :type common_only: boolean
    :return: possible timezones for the UTC offset
    :rtype: list of string
    """
    # pick one of the timezone collections
    timezones = pytz.common_timezones if common_only else pytz.all_timezones

    # convert the float hours offset to a timedelta
    offset_days, offset_seconds = 0, int(tz_offset * 3600)
    if offset_seconds < 0:
        offset_days = -1
        offset_seconds += 24 * 3600
    desired_delta = dt.timedelta(offset_days, offset_seconds)

    # Loop through the timezones and find any with matching offsets
    null_delta = dt.timedelta(0, 0)
    results = []
    for tz_name in timezones:
        tz = pytz.timezone(tz_name)
        non_dst_offset = getattr(tz, '_transition_info', [[null_delta]])[-1]
        if desired_delta == non_dst_offset[0]:
            results.append(tz_name)

    return results


def get_utc_offset_for_server(server):
    """
    Get the UTC offset for a Discord server.

    :param server: the Discord server
    :type server: discord.Server
    :return: the offset to add to the current system hour
    :rtype: int
    """
    offset = {
        'us-west': -8,
        'us-east': -5,
        'us-central': -6,
        'eu-west': +0,
        'eu-east': +2,
        'eu-central': +1,
        'singapore': +8,
        'london': +1,
        'sydney': +10,
        'amsterdam': +2,
        'frankfurt': +2,
        'brazil': -3,
        'vip-us-east': -5,
        'vip-us-west': -8,
        'vip-amsterdam': +2
    }[str(server.region)]

    new_offset = offset + (1 if is_dst(possible_timezones(offset)[0]) else 0)
    return new_offset


def get_12_hour_string(hour_24_hours):
    disc = 'AM' if hour_24_hours < 12 else 'PM'
    return '{}'.format(abs(hour_24_hours - 12)) + disc


class VoiceState:
    """The state of the bot's presence in a server."""

    def __init__(self, bot):
        """
        :param bot: the bot the state belongs to
        :type bot: discord.ext.commands.Bot
        """
        self.bot = bot
        self.server = None
        self.voice_client = None
        self.player = None
        self.last_checked_hour = None

    def is_playing(self):
        """
        Check if the bot is currently playing audio in a server.

        :return: true if the bot is playing audio
        :rtype: boolean
        """
        if self.voice_client is None or self.player is None:
            return False

        return self.player.is_playing()


class BotThread(Thread):
    def __init__(self, event, bot):
        Thread.__init__(self)
        self.stopped = event
        self.bot = bot

    def run(self):
        while not self.stopped.wait(10):
            self.bot.update_voice_clients()


class TownTuneBot(commands.Cog):
    """A bot to play the right ACNL song for the current hour in your server's region. Contains commands to start and
    stop the bot and periodically checks each active VoiceClient to see if its song needs to be changed or restarted.
    """

    def __init__(self, bot):
        """
        :param bot: the bot the state belongs to
        :type bot: discord.ext.commands.Bot
        """
        self.bot = bot
        self.voice_states = {}
        self.test_hour = None

        self.stopFlag = Event()
        self.update_thread = BotThread(self.stopFlag, self)
        self.update_thread.start()

    def get_voice_state(self, guild):
        """
        Get the VoiceState object for a server, or create one if it doesn't exist.

        :param guild: the server to get the VoiceState for.
        :return: the VoiceState object
        :rtype: VoiceState
        """
        state = self.voice_states.get(guild.id)
        if state is None:
            state = VoiceState(self.bot)
            state.guild = guild
            self.voice_states[guild.id] = state

        return state

    def schedule_voice_client_update(self, seconds):
        """
        Check each VoiceClient for possible updates.

        :param seconds: the time to sleep between checks
        :type seconds: int
        """
        time.sleep(seconds)
        self.update_voice_clients()
        return

    def update_voice_clients(self):
        """Check each active voice client to see if its song needs to be changed or restarted. """
        for guild_id, state in self.voice_states.items():
            if os.environ['ENV'] == 'development':
                logging.info('Updating client on server %s - %s', state.guild.id, state.guild.name)

            last_checked_hour = state.last_checked_hour
            current_server_hour = self.test_hour if self.test_hour is not None else (
                    dt.datetime.today() + dt.timedelta(hours=get_utc_offset_for_server(state.guild))).hour

            if last_checked_hour != current_server_hour:
                # If the hour has changed fade out the current song and play the hour chime.
                # The next song will start on the next voice start check.

                if os.environ['ENV'] == 'development':
                    logging.info('Changing to next song on server %s - %s', state.guild.id, state.guild.name)

                for i in range(0, 5):
                    state.voice_client.source.volume = state.voice_client.source.volume - 0.2
                    time.sleep(0.5)

                state.voice_client.stop()

                state.voice_client.play(discord.FFmpegPCMAudio('audio/hour-chime.mp3'))
                state.last_checked_hour = current_server_hour
            else:
                if not state.voice_client.is_playing():
                    # Start playing music if we aren't. This means the bot has recently connected to the server.

                    if os.environ['ENV'] == 'development':
                        logging.info('Restarting song on server %s - %s', state.guild.id, state.guild.name)

                    state.voice_client.play(discord.FFmpegPCMAudio('audio/{}.mp3'.format(current_server_hour)))
                    state.voice_client.source = discord.PCMVolumeTransformer(state.voice_client.source)
                    state.last_checked_hour = current_server_hour
                else:
                    if os.environ['ENV'] == 'development':
                        logging.info('Continuing song on server %s - %s', state.guild.id, state.guild.name)

    @commands.command(pass_context=True, no_pm=True)
    async def settesthour(self, ctx, *, hour: int = None):
        if os.environ['ENV'] == 'development':
            self.test_hour = hour
            logging.info('Test hour is now {}'.format(self.test_hour))
        else:
            logging.warning('Development command was tried in %s - %s', ctx.message.guild.name, ctx.message.guild.id)
            return False

    @commands.command(pass_context=True, no_pm=True)
    async def start(self, ctx):
        """
        Summon the bot to a voice channel. The voice client will be added to the voice states
        of the bot and will start playing on the next update.

        :param ctx: command context
        :type ctx: discord.ext.commands.Context
        :return: False if the command issuer is not in a voice channel
        """
        summoner = ctx.author.voice
        if summoner is None:
            logging.warning('User is not in a voice channel')
            await ctx.send('You must be in a voice channel to start TownTune')
            return False

        state = self.get_voice_state(ctx.message.guild)
        if state.voice_client is None:
            state.voice_client = await summoner.channel.connect()
        else:
            await state.voice_client.move_to(summoner.channel)

        current_server_hour = (dt.datetime.today()
                               + dt.timedelta(hours=get_utc_offset_for_server(ctx.message.guild))).hour
        state.last_checked_hour = current_server_hour

    @commands.command(pass_context=True, no_pm=True)
    async def stop(self, ctx):
        """
        Stops the bot playing music and disconnects it from a voice channel.

        :param ctx: command context
        :type ctx: discord.ext.commands.Context
        :return:
        """
        guild = ctx.message.guild
        state = self.get_voice_state(guild)

        if state.is_playing():
            player = state.player
            player.stop()

        if state.voice_client is not None:
            await state.voice_client.disconnect()
            del self.voice_states[guild.id]


bot = commands.Bot(command_prefix=commands.when_mentioned, description="Welcome to Animal Crossing!")
town_tune_bot = TownTuneBot(bot)
bot.add_cog(town_tune_bot)


@bot.event
async def on_ready():
    """Called when the bot successfully logs in."""
    logging.info('Logged in as:%s (ID: %s)', bot.user, bot.user.id)


bot.run(os.environ['BOT_TOKEN'])
