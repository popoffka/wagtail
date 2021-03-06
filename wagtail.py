import curses

import os
import os.path
import queue
import sys
import termios
import time

from datetime import datetime

from zpipe.python import zpipe

from configmanager import ConfigManager
from db import Database
from ui.commandline import CommandLine, parse_cmdline_into_events
from ui.composer import ZephyrgramComposer
from ui.mainwindow import MainWindow
from ui.statusbar import StatusBar
from util import get_principal, take_unprefix
from zephyrgram import Zephyrgram

# value assigned to the SIGWINCH signal in Linux on x86, arm, sparc and
# most other architectures, according to the manpage signal(7)
SIGWINCH = 28

class Wagtail:
    def __init__(self):
        self.principal = get_principal()

        self.config = ConfigManager(self)
        self.db = Database(self)

        self.zgram_queue = queue.Queue()
        self.error_queue = queue.Queue()

        def zgram_handler(zp, zgram):
            self.zgram_queue.put(zgram)
            # we send a SIGWINCH signal to ourselves,
            # making ncurses think that the window was resized.
            # this is the best way I know of to interrupt ncurses
            # in blocking mode.
            os.kill(os.getpid(), SIGWINCH)
        def error_handler(error):
            self.error_queue.put(error)
            os.kill(os.getpid(), SIGWINCH)


        self.zpipe = zpipe.ZPipe(['./zpipe/zpipe'],
            zgram_handler, error_handler)

        self.zpipe.subscribe('message', '*', self.principal)
        for class_, instance, recipient, _ in self.db.get_subscriptions():
            self.zpipe.subscribe(class_, instance, recipient)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.db.close()
        self.zpipe.close()

    def get_config_path(self):
        if os.getenv('XDG_CONFIG_HOME') is not None:
            return os.path.join(os.getenv('XDG_CONFIG_HOME'), 'wagtail',
                'config.py')
        return os.path.expanduser('~/.config/wagtail/config.py')

    def get_database_path(self):
        if os.getenv('XDG_CONFIG_HOME') is not None:
            return os.path.join(os.getenv('XDG_CONFIG_HOME'), 'wagtail',
                'database.sqlite3')
        return os.path.expanduser('~/.config/wagtail/database.sqlite3')

    def event_quit(self):
        self.should_quit = True

    def event_cmdline_open(self, initial_input=''):
        self.window_stack.append(CommandLine(self, initial_input))

    def event_cmdline_close(self):
        assert isinstance(self.window_stack[-1], CommandLine)
        self.window_stack.pop().close()

    def event_cmdline_exec(self, cmdline):
        events = parse_cmdline_into_events(cmdline)
        self.handle_events(events)

    def event_status(self, text):
        self.status_bar.set_status(text)

    def event_zwrite(self, opts):
        if ((opts.class_.lower() == 'message') and
            ((opts.instance.lower() == 'personal') or
            (opts.instance.lower() == 'urgent')) and
            (len(opts.recipients) == 0)):
            self.status_bar.set_status(
                'Cannot send personal message with no recipient.')
        else:
            self.window_stack.append(ZephyrgramComposer(self, opts))

    def event_composer_close(self):
        assert(isinstance(self.window_stack[-1], ZephyrgramComposer))
        self.window_stack.pop().close()

    def event_send_zephyrgrams(self, zgrams):
        for zgram in zgrams:
            self.zpipe.zwrite(zgram.to_zpipe())

            if zgram.is_personal():
                # this is a personal message. we won't get it back, so
                # we should save a copy.
                # TODO: what if the zgram actually fails to send?
                zgram.time = zgram.time or datetime.now()
                zgram.sender = zgram.sender or self.principal
                self.db.append_message(zgram)

        self.main_window.redraw() # to display the personals

    def event_subscribe(self, class_, instance, recipient):
        new_subs = self.db.subscribe(class_, instance, recipient)

        if len(new_subs) == 0:
            self.status_bar.set_status('Error: Already subscribed.')
        else:
            for args in new_subs:
                self.zpipe.subscribe(*args)
            self.status_bar.set_status('Subscribed successfully.')

    def event_unsubscribe(self, class_, instance, recipient):
        unsubs = self.db.unsubscribe(class_, instance, recipient)

        if len(unsubs) == 0:
            self.status_bar.set_status(
                ('Error: either not subscribed, or trying to unsubscribe '
                 'from unclass.'))
        else:
            for args in unsubs:
                self.zpipe.unsubscribe(*args)
            self.status_bar.set_status('Unsubscribed successfully.')

    def event_import_zsubs(self, path):
        if path is None:
            path = os.path.expanduser('~/.zephyr.subs')

        try:
            with open(path) as zsubs:
                processed = 0
                skipped = 0
                for line in zsubs:
                    if len(line.strip()) == 0:
                        continue

                    parts = line.strip().split(',')
                    if len(parts) != 3:
                        skipped += 1
                    else:
                        for sub in self.db.subscribe(*parts):
                            self.zpipe.subscribe(*sub)
                        processed += 1

            self.status_bar.set_status(
                'Done, {} lines processed successfuly, {} skipped.'.format(
                    processed, skipped))
        except FileNotFoundError:
            self.status_bar.set_status(
                'Error: file {} not found.'.format(path))

    def event_reload_config(self):
        self.config.reload()
        self.main_window.redraw() # styling rules might've changed

    def event_filter(self, new_filter):
        self.main_window.set_filter(new_filter)

    def handle_events(self, events):
        for event, *event_args in events:
            # call self.event_{eventname}(*event_args)
            getattr(self, 'event_{}'.format(event))(*event_args)

    def main_curses(self, screen):
        # tell the terminal to not send us SIGINTs when Ctrl+C is pressed
        tty_attributes = termios.tcgetattr(sys.stdin)
        tty_attributes[3] &= ~termios.ISIG
        termios.tcsetattr(sys.stdin, termios.TCSANOW, tty_attributes)

        self.screen = screen

        curses.use_default_colors()
        curses.curs_set(0)

        self.status_bar = StatusBar(self)
        self.main_window = MainWindow(self)
        curses.doupdate()

        # this window stack kind of duplicates the one kept by
        # curses.panel — perhaps we should just use that directly?
        self.window_stack = [self.status_bar, self.main_window]

        self.should_quit = False

        while True:
            key = screen.get_wch() # blocks indefinitely

            if key == curses.KEY_RESIZE:
                # this could be either a legitimate SIGWINCH, notifying us that
                # the terminal was resized,
                # or a fake SIGWINCH, generated by the zgram_handler
                # (see __init__) notifying us that we have new zephyrgrams
                # to take from the queue
                while not self.zgram_queue.empty():
                    zgram = Zephyrgram.from_zpipe(self.zgram_queue.get())
                    self.db.append_message(zgram)

                    # if this is in class 'ununclass', and we aren't yet
                    # subscribed to 'unununclass', do so
                    undepth, class_stripped = take_unprefix(zgram.class_)
                    new_unclass = 'un' + zgram.class_
                    for instance, recipient in self.db.update_undepth(
                        class_stripped, undepth + 1):
                        self.zpipe.subscribe(new_unclass, instance, recipient)

                for window in self.window_stack:
                    window.update_size()
            else:
                self.status_bar.clear_status()

                self.should_quit = False

                events = self.window_stack[-1].handle_keypress(key)
                self.handle_events(events)

                if self.should_quit:
                    break

            if not self.error_queue.empty():
                error = self.error_queue.get()
                self.status_bar.set_status('Error in {}: {}'.format(
                    error.operation, error.message))

            curses.doupdate()


    def main(self):
        curses.wrapper(self.main_curses)

if __name__ == '__main__':
    with Wagtail() as wag:
        wag.main()
