import os
import sys
import re
import time
import glob
import pickle
import logging
import inspect
from functools import wraps

import yaml
import requests

from discord import errors, Message
from discord.ext import commands
from discord.utils import get
from asyncio import sleep

from utils import *
from utils.embeds import refresh_embed, add_source, remove_source
from utils.user import download_users_list

logger = setup_logger(__name__, "bot")

def log(func):
    """Decorator for functions, to log start/end times"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        id = uuid.get_and_increment()
        logger.debug(f"{func.__name__} (f{id}) called")
        if inspect.iscoroutinefunction(func):
            result = await func(*args, **kwargs)
        else:
            result = func(*args, **kwargs)
        logger.debug(f"{func.__name__} (f{id}) finished")
        return result
    return wrapper


class FileState():
    """Parent class for managing file states"""
    def __init__(self, filepath):
        self.filepath = filepath

    def file_exists(self):
        return os.path.exists(self.filepath)

class IdProcessManager(FileState):
    """Manage tracking the current mal.py process"""

    def __init__(self, *, filepath="pid", manager_python_file=None):
        """
        filepath: file that contains the current pid
        manager_python_file: file that manages pulling new IDs from github and creating pickle embeds
        """
        super().__init__(filepath)
        self.process_filepath = mal_fp
        self.pid = None

    @log
    def poll(self):
        """If the process doesn't exist start it"""
        if not self.file_exists():
            return self._call_process()
        else:
            self.read_from_pid_file()
            if not self.check_pid():
                return self._call_process()

    @log
    def read():
        with open(self.filepath, 'r') as pid_f:
            self.pid = int(pid_f.read())

    @log
    def _call_process():
        """Call the process that checks for new IDs/generates embeds as a background process"""
        os.system("python3 {} &".format(self.process_filepath))

    @log
    def check_pid():
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        else:
            return True


class OldDatabase(FileState):
    """Models and interacts with the 'old' database file"""

    def __init__(self, *, filepath="old"):
        super().__init__(filepath)

    @log
    def read():
        with open(self.filepath, 'r') as old_f:
            return set(old_f.read().splitlines())

    @log
    def dump(contents):
        contents = sorted(list(contents), key=int)
        with open(self.filepath, 'w') as old_f:
            old_f.write("\n".join(contents))


class NewEntries(FileState):
    """Models and interacts with the 'new' pickles file"""

    def __init__(self, *, filepath="new"):
        super().__init__(filepath)

    @log
    def read():
        with open(self.filepath, 'rb') as new_f:
            return pickle.load(new_f)

    def remove():
        if self.file_exists():
            os.remove(self.filepath)

def is_admin_or_owner():
    """Check that returns True if the user is the owner/admin on the server"""
    async def predicate(ctx):
        is_owner = await ctx.bot.is_owner(ctx.author)
        is_admin = await ctx.author.permissions_in(ctx.channel).administrator
        return is_owner or is_admin
    return commands.check(predicate)

def has_privilege():
    """Check that returns true if the user can use 'trusted' commands or is owner/admin"""
    async def predicate(ctx):
        is_owner = await self.is_owner(ctx.author)
        is_admin = await ctx.author.permissions_in(ctx.channel).administrator
        is_trusted = "trusted" in [role.name.lower() for role in ctx.author.roles]
        return is_owner or is_admin or is_trusted
    return commands.check(predicate)


class MalNotifyBot(commands.Bot):

    def __init__(self, *args, **kwargs):

        # configuration variables modified in on_ready
        self.feed_channel = None
        self.nsfw_feed_channel = None
        self.old_db = None
        self.new_entries = None
        self.process_manager = None
        self.period = 60  # how often (in seconds) to check if there are new entries
        super().__init__(*args, **kwargs)
        self.loop_check = self.loop.create_task(self.loop())


    async def on_ready(self):
        guilds = list(iter(client.guilds))
        if len(guilds) != 1:
            logger.critical("This bot should only be used on one server")
            await self.logout()
            sys.exit(1)
        channels = guilds[0].channels
        self.feed_channel = get(channels, name="feed")
        self.nsfw_feed_channel = get(channels, name="nsfw-feed")
        if self.feed_channel is None:
            logger.critical("Couldn't find the 'feed' channel")
        if self.nsfw_feed_channel is None:
            logger.critical("Couldn't find the 'nsfw-feed' channel")
        self.old_db = OldDatabase(filepath="old")
        self.new_entries = NewEntries(filepath="new")
        self.process_manager = IdProcessManager(filepath="pid", manager_python_file="mal.py")
        self.process_manager.poll()


    # override on_message so we can remove double spaces after the bot name,
    # which would ordinarily not trigger commands
    async def on_message(self, message):
        message.content = re.sub("\s{2,}", " ", message.content) # remove weird spaces
        await self.process_commands(message)

    @log
    async def search_feed_for_mal_id(mal_id, channel, limit) -> Message:
        """
        checks a feed channel (which is filled with embeds) for a message
        returns the discord.Message object if it finds it within limit, else return None
        """
        async for message in channel.history(limit=limit, oldest_first=False):
            try:
                embed = message.embeds[0]
                embed_id = extract_mal_id_from_url(embed.url)
                if embed_id is not None and embed_id == mal_id:
                    return message
            except Exception as e:
                logger.warning("Error while searching history: {}".format(str(e)))
                continue
        return None  # if we've exited the loop

    # run in event loop
    @log
    async def loop():
        await self.wait_until_ready()
        while not self.is_closed():
            # check if scraper is running
            self.process_manager.poll()
            # if there are new entries print them
            if self.new_entries.file_exists():
                await self.add_new_entries()
            # check for 'new' file periodically
            await sleep(self.period)

    @log
    async def add_new_entries():
        pickles = self.new_entries.read()
        old_entries = self.old_db.read()
        for new, sfw in pickles:
            if new not in old_entries: # makes sure we're not printing entries twice
                if sfw:
                    await feed_channel.send(embed=new)
                else:
                    await nsfw_feed_channel.send(embed=new)
                # make sure that we actually printed it and it didn't fail due to network issues
                new_mal_id = extract_mal_id_from_url(new.url)
                check_channel = feed_channel if sfw else nsfw_feed_channel
                result = await self.search_feed_for_mal_id(mal_id=new_mal_id, channel=check_channel, limit=50)
                if result: # if we found the corresponding embed
                    old_entries.add(new_mal_id)
        self.old_db.dump(old_entries)
        # if id's weren't found in the 'search_feed_for_mal_id', the id will remain not in old_entries
        # and a new embed will be generated next time the loop in mal.py runs
        self.new_entries.remove() # remove new entries file as we've logged them


    @commands.command()
    @has_privilege()
    @log
    async def add_new(ctx):
        if self.new_entries.file_exists():
            await self.add_new_entries()
        else:
            await ctx.channel.send("No new entries found.")


    @commands.command()
    @log
    @is_admin_or_owner
    async def test_log(ctx):
        await client.send_message(feed_channel, "test message. beep boop")
        await client.send_message(nsfw_feed_channel, "test message. beep boop")


    @commands.command()
    @log
    @has_privilege()
    async def source(ctx, mal_id: int, *, links):

        adding_source = True
        if links.strip().lower() == "remove":
            adding_source = False

        if adding_source:
            valid_links = []
            # if there are multiple links, check each
            for link in links.split():
                # remove supression from link, if it exists
                link = remove_discord_link_supression(link)

                # test if link exists; blocking
                try:
                    resp = requests.get(link)
                except requests.exceptions.MissingSchema:
                    return await ctx.channel.send("`{}` has no schema (e.g. https), its not a valid URL.".format(link))
                if not resp.status_code == requests.codes.ok:
                    return await ctx.channel.send("Error connecting to <{}> with status code {}".format(link, resp.status_code))
                valid_links.append(link)

        # get logs from feed
        message = await self.search_feed_for_mal_id(str(mal_id), self.feed_channel, limit=999999)
        if not message:
            return await ctx.channel.send("Could not find a message that conatins the MAL id {} in {}".format(mal_id, feed_channel.mention))
        else:
            if adding_source:
                new_embed, is_new_source = add_source(embed, valid_links)
                await message.edit(embed=new_embed)
                return await ctx.channel.send("{} source for '{}' successfully.".format("Added" if is_new_source else "Replaced", embed.title))
            else:
                new_embed = remove_source(embed)
                await message.edit(embed=new_embed)
                return ctx.channel.send("Removed source for '{}' successfully.".format(embed.title))


    @commands.command()
    @log
    @has_privilege()
    async def refresh(ctx, mal_id: int):
        remove_image = "remove image" in ctx.message.content.lower()
        message = await self.search_feed_for_mal_id(str(mal_id), feed_channel, limit=999999)
        if not message:
            return await ctx.channel.send("Could not find a message that conatins the MAL id {} in {}".format(mal_id, feed_channel.mention))
        else:
            new_embed = refresh_embed(embed, mal_id, remove_image)
            await message.edit(embed=new_embed)
            return await ctx.channel.send("{} for '{}' successfully.".format("Removed image" if remove_image else "Updated fields", embed.title))


    @commands.command()
    @log
    async def check(ctx, mal_username, num: int):
        # the request.get calls are synchronous - blocking, figure out a better way to implement this
        return await ctx.channel.send("This command is currently disabled.")
        leftover_args = " ".join(ctx.message.content.strip().split()[4:])
        print_all = "all" in leftover_args.lower()
        message = await ctx.channel.send("Downloading {}'s list (downloaded 0 anime entries...)".format(mal_username))
        parsed = {}
        for entries in download_users_list(mal_username):
            for e in entries:
                parsed[e['mal_id']] = e['watching_status']
            await message.edit(content=f"Downloading {mal_username}'s list (downloaded {len(parsed)} anime entries...)")
        found_entry = False # mark True if we find an entry the user hasnt watched
        async for message in feed_channel.history(limit=num, oldest_first=False):
            try:
                embed = message.embeds[0]
            except Exception as e:
                continue
            source_exists = "Source" in [f.name for f in embed.fields]
            if source_exists or print_all:
                m = re.search("https:\/\/myanimelist\.net\/anime\/(\d+)", embed.url)
                mal_id = int(m.group(1))
                on_your_list = mal_id in parsed
                on_your_ptw = mal_id in parsed and parsed[mal_id] == "6"
                if (not on_your_list) or (on_your_ptw and source_exists):
                    found_entry = True
                    if source_exists:
                        fixed_urls = " ".join(["<{}>".format(url) for url in [f.value for f in embed.fields if f.name == "Source"][0].split()])
                        if on_your_ptw:
                            await ctx.channel.send("{} is on your PTW, but it has a source: {}".format(embed.url, fixed_urls))
                        else:
                            await ctx.channel.send("{} isn't on your list, but it has a source: {}".format(embed.url, fixed_urls))
                    else:
                        await ctx.channel.send("{} isn't on your list.".format(embed.url))

        if not found_entry:
            await ctx.channel.send("I couldn't find any MAL entries in the last {} entries that aren't on your list.".format(num))


    @commands.command()
    async def help(ctx):
       #"`check`: checks if you've watched the 'n' most recent entries in {}\n".format(feed_channel.mention) + \
       #"\tSyntax: `@notify check <mal_username> <n>`" + \
       #"\n\tExample `@notify check purplepinapples 10`\n" + \
       #"\tBy default, this will only print entries that have sources, provide the `all` keyword to make it check all entries:\n" + \
       #"\t\tExample: `@notify check purplepinapples 10 all`\n" + \
        help_str = "**User commands**:\n`help`: describe commands\n" + \
                   "**Trusted commands**:\n" + \
                   "`source`: Adds a link to a embed in {}\n\tSyntax: `@notify source <mal_id> <link>`".format(feed_channel.mention) + \
                   "\n\tExample: `@notify source 32287 https://www.youtube.com/watch?v=1RzNDZFQllA`\n" + \
                   "\t`@notify source <mal_id> remove` will remove a source from an embed\n" + \
                   "`add_new`: Checks if there are new entries waiting to be printed. This happens once every 3 minutes automatically\n" + \
                   "`refresh`: Refreshes the image and synopsis on an embed\n\tExample: `@notify refresh 39254`\n" + \
                   "\tYou can do `@notify refresh <mal_id> remove image` to remove the image from an embed (if it happens to be the placeholder MAL image)\n" + \
                   "**Administrator commands**:\n" + \
                   "`test_log`: send a test message to {} to check permissions\n".format(feed_channel.mention)
        await ctx.channel.send(help_str)


    async def on_command_error(ctx, error):

        command_name = ctx.command.name
        clean_message_content = ctx.message.content.split(">", maxsplit=1)[1].strip().replace("`", '')
        args = clean_message_content.split()

        # prevent self-loops; on_command_error calling on_command_error
        if hasattr(ctx.command, 'on_error'):
            logger.warning("on_command_error self loop occured")
            return

        if isinstance(error, commands.CommandNotFound):
            await ctx.channel.send("Could not find the command `{}`. Use `@notify help` to see a list of commands.".format(command_name))
        elif isinstance(error, commands.CheckFailure):
            await ctx.channel.send("You don't have sufficient permissions to run this command.")
        elif isinstance(error, commands.MissingRequiredArgument) and command_name == "source":
            await ctx.channel.send("You're missing one or more arguments for the `source` command.\nExample: `@notify source 31943 https://youtube/...`")
        elif isinstance(error, commands.MissingRequiredArgument) and command_name == "refresh":
            await ctx.channel.send("Provide the MAL id you wish to refresh the embed for.")
        elif isinstance(error, commands.BadArgument) and command_name in ["source", "refresh"]:
            try:
                int(args[1])
            except ValueError:
                await ctx.channel.send("Error converting `{}` to an integer.".format(args[1]))
        elif isinstance(error, commands.MissingRequiredArgument) and command_name == "check":
            await ctx.channel.send("Provide your MAL username and then the number of entries in {} you want to check".format(feed_channel.mention))
        elif isinstance(error, commands.BadArgument) and command_name == "check":
            try:
                int(args[2])
            except:
                await ctx.channel.send("Error converting `{}` to an integer.".format(args[2]))
        elif isinstance(error, commands.CommandInvokeError):
            original_error = error.original
            if isinstance(original_error, errors.HTTPException):
                await ctx.channel.send("There was an issue connecting to the Discord API. Wait a few moments and try again.")
            elif isinstance(original_error, RuntimeError):
                await ctx.channel.send(str(original_error)) # couldn't find a user with that username
        else:
            await ctx.channel.send("Uncaught error: {}: {}".format(type(error).__name__, error))
            raise error # caught and printed on stderr

if __name__ == "__main__":

    # Token is stored in token.yaml, with the key 'token'
    with open('token.yaml', 'r') as t:
        token = yaml.load(t, Loader=yaml.FullLoader)["token"]

    bot = MalNotifyBot(command_prefix=commands.when_mentioned, case_insensitive=False)
    bot.run(token, bot=True, reconnect=True)
