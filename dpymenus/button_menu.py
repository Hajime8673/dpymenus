import asyncio
import logging
from typing import Dict, Optional, TYPE_CHECKING

import emoji
from discord import Emoji, Message, PartialEmoji, RawReactionActionEvent, Reaction
from discord.abc import GuildChannel
from discord.ext.commands import Context

from dpymenus import BaseMenu
from dpymenus.exceptions import ButtonsError, EventError, SessionError
from dpymenus.settings import HIDE_WARNINGS

if TYPE_CHECKING:
    from dpymenus.types import Button


class ButtonMenu(BaseMenu):
    """Represents a button-based response menu."""

    _data: Dict

    def __init__(self, ctx: Context):
        super().__init__(ctx)

    def __repr__(self):
        return f"ButtonMenu({self.ctx})"

    @property
    def data(self) -> Dict:
        return getattr(self, "_data", {})

    def set_data(self, data: Dict) -> "ButtonMenu":
        """Sets a dictionary up for persistent state data. Returns itself for fluent-style chaining."""
        self._data = data

        return self

    def button_pressed(self, button: "Button") -> bool:
        """Checks if the reaction the user pressed is equal to the argument."""
        return button == self.input

    async def open(self):
        """The entry point to a new ButtonMenu instance; starts the main menu loop. Manages collecting user input,
        validation, sending messages, and cancellation requests."""
        try:
            self._validate_buttons()
            await super()._open()

        except (ButtonsError, EventError) as exc:
            logging.error(exc.message)

        except SessionError as exc:
            logging.info(exc.message)

        else:
            await self._add_buttons()
            _first_iter = True

            while self.active:
                if _first_iter is False:
                    if self.last_visited_page() != self.page.index:
                        await self._add_buttons()
                    else:
                        if isinstance(self.output.channel, GuildChannel):
                            await self.output.remove_reaction(self.input, self.ctx.author)

                # refresh our message content with the new reactions added; this is an API hit
                self.output = await self.destination.fetch_message(self.output.id)

                self.input = await self._get_input()

                if self.input:
                    await self.page.on_next_event(self)

                    if self.last_visited_page() != self.page.index:
                        await self._cleanup_reactions()

                _first_iter = False

    # Internal Methods
    async def _shortcircuit(self):
        """Runs a background loop to poll the menus `active` state. Returns when False. Allows for short-circuiting
        the main loop when it is waiting for user reaction events from discord.py."""
        while self.active:
            await asyncio.sleep(1)
        else:
            return

    async def _get_input(self) -> Optional[Message]:
        """Waits for a user reaction input event and returns the message object."""
        tasks = [
            asyncio.create_task(task())
            for task in [self._get_reaction_add, self._get_reaction_remove, self._shortcircuit]
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=self.timeout)

        # if all tasks are still pending, we force a timeout by manually calling cleanup methods
        if len(pending) == len(tasks):
            await self._timeout()
        else:
            # we need to cancel tasks first
            for task in pending:
                task.cancel()

            for future in done:
                result = future.result()
                if result:
                    self.input = result
                    break
                else:
                    return

    async def _add_buttons(self):
        """Adds reactions to the message object based on what was passed into the page buttons."""
        for button in self.page.buttons_list:
            await self.output.add_reaction(button)

    async def _get_reaction_add(self) -> Optional["Button"]:
        """Waits for a user reaction add event and returns the event object."""
        try:
            event = await self.ctx.bot.wait_for(
                "raw_reaction_add",
                timeout=self.timeout,
                check=self.custom_check if self.custom_check else self._check_reaction,
            )

        except asyncio.TimeoutError:
            await self._timeout()

        else:
            return await self._get_emoji(event)

    async def _get_reaction_remove(self) -> Optional["Button"]:
        """Waits for a user reaction remove event and returns the event object."""
        try:
            event = await self.ctx.bot.wait_for(
                "raw_reaction_remove",
                check=self.custom_check if self.custom_check else self._check_reaction,
            )

        except asyncio.TimeoutError:
            await self._timeout()

        else:
            return await self._get_emoji(event)

    async def _get_emoji(self, reaction_event: RawReactionActionEvent) -> "Button":
        """Returns an emoji object from a raw reaction event."""
        for btn in self.page.buttons_list:
            if isinstance(btn, Emoji):
                if btn == reaction_event.emoji:
                    return btn

            elif isinstance(btn, str):
                # split the str and test if the value between ':' is the same as the PartialEmoji name
                _test = btn.split(":")
                if len(_test) > 1:
                    if _test[1] == reaction_event.emoji.name:
                        return btn

                else:
                    if btn == reaction_event.emoji.name:
                        return btn

            else:
                return reaction_event.emoji

    async def _safe_clear_reactions(self):
        """Removes all reactions from the output message object if the bot has permissions."""
        if self.output and isinstance(self.output.channel, GuildChannel):
            await self.output.clear_reactions()

    def _check_reaction(self, event: RawReactionActionEvent) -> bool:
        """Returns true if the event author is the same as the initial value in the menu context. Additionally,
        checks if the reaction is a valid button (and not a user added reaction)."""
        # very cursed code...
        return (
            event.user_id == self.ctx.author.id
            and event.message_id == self.output.id
            and any(
                event.emoji.name == btn
                for btn in [
                    (reaction.emoji.name if isinstance(reaction.emoji, Emoji) else reaction.emoji)
                    if isinstance(reaction, Reaction)
                    else reaction
                    for reaction in self.output.reactions
                ]
            )
        )

    # Validation Checks
    def _validate_buttons(self):
        """Ensures that a menu was passed the appropriate amount of buttons."""
        _cb_count = 0
        for page in self.pages:
            if not page.buttons_list:
                break

            if page.on_next_event:
                _cb_count += 1

            if len(page.buttons_list) < 1:
                raise ButtonsError(
                    "Any page with an `on_next` event capture must have at least one button.\n"
                    f"{page} {page.title} only has {len(page.buttons_list)} buttons."
                )

            if len(page.buttons_list) > 5 and HIDE_WARNINGS is False:
                logging.warning(
                    "Adding more than 5 buttons to a page at once may result in discord.py throttling the bot client."
                )

            self._check_buttons(page.buttons_list)

        if self.page.on_fail_event:
            raise EventError("A ButtonMenu can not capture an `on_fail` event.")

        if _cb_count < len(self.pages) - 1:
            raise EventError(
                f"ButtonMenu missing `on_next` captures. Expected {len(self.pages) - 1}, found {_cb_count}."
            )

    def _check_buttons(self, buttons_list):
        """Checks the button list for valid emoji unicode or Discord emojis."""
        for button in buttons_list:
            if isinstance(button, (Emoji, PartialEmoji)):
                continue

            if isinstance(button, str):
                # split the str and test if the value between ':' is in the bot list
                if _test := button.split(":"):
                    if len(_test) > 1 and _test[1] in [e.name for e in self.ctx.bot.emojis]:
                        continue

                if _test := emoji.UNICODE_EMOJI_ALIAS_ENGLISH.get(button, None) is not None:
                    continue

            raise ButtonsError(f"Invalid Emoji or unicode string: {button}")
