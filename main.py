from dotenv import load_dotenv
load_dotenv()

import os
import re
import sqlite3
from datetime import datetime, date
import asyncio
from dateutil.relativedelta import relativedelta
import random
import sys
import google.generativeai as genai
import logging
import requests
import html

from fastapi import FastAPI, Request
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from slack_sdk.errors import SlackApiError

# --- Basic Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --- MOVED UP: FILE LOADING FUNCTION ---
# This must be defined before it is used by the constants below.
def load_word_list(filename):
    """Loads a list of words from a file."""
    try:
        with open(filename, 'r') as f:
            words = [line.strip().upper() for line in f if line.strip()]
        if not words:
            logger.critical(f"Word file '{filename}' is empty. The bot cannot function.")
            sys.exit(1)
        return words
    except FileNotFoundError:
        logger.critical(f"The required word file '{filename}' was not found. Please create it.")
        sys.exit(1)


# --- GAME CONSTANTS & STATE ---
# Now this section can safely call the function defined above.
active_games = {}
WORDLE_ANSWERS = load_word_list("wordle_answers.txt")
VALID_GUESSES = set(load_word_list("valid_guesses.txt"))
VALID_GUESSES.update(WORDLE_ANSWERS)


# --- INITIALIZE GEMINI API ---
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    logger.info("Gemini API configured successfully.")
except Exception as e:
    logger.critical(f"Could not configure Gemini API. Error: {e}")
    gemini_model = None

# --- DATABASE SETUP ---
def setup_database():
    conn = sqlite3.connect('slack_bot.db'); cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS birthdays (user_id TEXT PRIMARY KEY, birthday_date TEXT NOT NULL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS anniversaries (user_id TEXT PRIMARY KEY, anniversary_date TEXT NOT NULL)')
    cursor.execute('CREATE TABLE IF NOT EXISTS settings_birthday (id INTEGER PRIMARY KEY, announcement_channel TEXT, announcement_time TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS settings_anniversary (id INTEGER PRIMARY KEY, announcement_channel TEXT, announcement_time TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS settings_game (id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL)')
    conn.commit(); conn.close()
    logger.info("Database setup complete.")

# --- SLACK APP & FASTAPI SETUP ---
slack_app = AsyncApp(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)
api_app = FastAPI()
slack_handler = AsyncSlackRequestHandler(slack_app)
scheduler = AsyncIOScheduler()

# --- DB & HELPER FUNCTIONS ---
def db_write(q, p=()): conn = sqlite3.connect('slack_bot.db'); c=conn.cursor(); c.execute(q, p); conn.commit(); conn.close()
def db_read_one(q, p=()): conn = sqlite3.connect('slack_bot.db'); c=conn.cursor(); c.execute(q, p); return c.fetchone()
def db_read_all(q, p=()): conn = sqlite3.connect('slack_bot.db'); c=conn.cursor(); c.execute(q, p); return c.fetchall()
def db_reset(): conn = sqlite3.connect('slack_bot.db'); c=conn.cursor(); c.execute("DELETE FROM birthdays"); c.execute("DELETE FROM anniversaries"); c.execute("DELETE FROM settings_birthday"); c.execute("DELETE FROM settings_anniversary"); c.execute("DELETE FROM settings_game"); conn.commit(); conn.close()


async def is_user_admin(client, user_id):
    try: user_info = await client.users_info(user=user_id); return user_info['user'].get('is_admin', False) or user_info['user'].get('is_owner', False)
    except SlackApiError: return False
async def get_user_info(client, user_id):
    try: return await client.users_info(user=user_id)
    except SlackApiError: return None
async def get_user_date_format(client, user_id):
    user_info = await get_user_info(client, user_id)
    if user_info and user_info.get('ok') and user_info.get('user', {}).get('tz', '').lower().startswith('america'): return ('MM-DD', 'e.g., 08-27')
    return ('DD-MM', 'e.g., 27-08')
async def get_channel_name(client, channel_id):
    try: info = await client.conversations_info(channel=channel_id); return info['channel']['name']
    except Exception: return channel_id


# ---------------------------------------------------------------------
# --- GAME FRAMEWORK & IMPLEMENTATIONS ---
# ---------------------------------------------------------------------

# --- Game 1: Wordle ---
async def start_wordle_game(user_id, client):
    word_of_the_day = await get_word_of_the_day()
    game_state = {'game_name': 'wordle', 'state': {'word': word_of_the_day, 'guesses': []}}
    active_games[user_id] = game_state
    logger.info(f"Starting Wordle game for {user_id}. Word is {word_of_the_day}.")
    initial_message = ("Happy Birthday! :tada: For a bit of fun, let's play a game of *Wordle*!\n\n"
                       "Guess the *5-letter word* in 6 tries.\n"
                       "Reply with your first guess. Good luck!")
    await client.chat_postMessage(channel=user_id, text=initial_message)

async def handle_wordle_guess(guess, user_id, game_session, say):
    if len(guess) != 5 or not guess.isalpha():
        await say("That's not a 5-letter word. Please try again.")
        return
    if guess not in VALID_GUESSES:
        is_real = await is_real_word_with_ai(guess)
        if not is_real:
            await say("That word is not in my dictionary. Please try again.")
            return
    game_state = game_session['state']
    target_word = game_state["word"]
    game_state["guesses"].append(guess)
    history_text = "".join(f"`{g}` -> {evaluate_guess(g, target_word)}\n" for g in game_state["guesses"])
    if guess == target_word:
        await say(f"{history_text}\nCongratulations, you guessed it! The word was *{target_word}*! :tada:")
        del active_games[user_id]
    elif len(game_state["guesses"]) >= 6:
        await say(f"{history_text}\nNice try! You've used all your guesses. The word was *{target_word}*. Better luck next time!")
        del active_games[user_id]
    else:
        remaining = 6 - len(game_state["guesses"])
        await say(f"{history_text}\nYou have {remaining} guess(es) left.")


# --- Game 2: Number Guesser ---
async def start_number_guesser_game(user_id, client):
    target_number = random.randint(1, 100)
    game_state = {'game_name': 'number_guesser', 'state': {'target': target_number, 'guesses': 0, 'limit': 7}}
    active_games[user_id] = game_state
    logger.info(f"Starting Number Guesser game for {user_id}. Target is {target_number}.")
    initial_message = (f"Happy Birthday! :tada: For a bit of fun, let's play *Higher or Lower*!\n\n"
                       f"I'm thinking of a number between 1 and 100. You have {game_state['state']['limit']} tries to guess it.\n"
                       "What's your first guess?")
    await client.chat_postMessage(channel=user_id, text=initial_message)

async def handle_number_guesser_guess(guess_text, user_id, game_session, say):
    try:
        guess = int(guess_text)
    except ValueError:
        await say("That's not a number! Please guess a number between 1 and 100.")
        return
    game_state = game_session['state']
    game_state['guesses'] += 1
    target = game_state['target']
    if guess == target:
        await say(f"You got it! The number was *{target}*. You guessed it in {game_state['guesses']} tries! :confetti_ball:")
        del active_games[user_id]
    elif game_state['guesses'] >= game_state['limit']:
        await say(f"Nice try! You're out of guesses. The number I was thinking of was *{target}*. Better luck next time!")
        del active_games[user_id]
    elif guess < target:
        await say(f"*{guess}* is too low. Guess *higher*! You have {game_state['limit'] - game_state['guesses']} tries left.")
    elif guess > target:
        await say(f"*{guess}* is too high. Guess *lower*! You have {game_state['limit'] - game_state['guesses']} tries left.")


# --- Game 3: Trivia ---
async def start_trivia_game(user_id, client):
    try:
        response = requests.get("https://opentdb.com/api.php?amount=1&type=multiple")
        response.raise_for_status()
        data = response.json()['results'][0]
        question = html.unescape(data['question'])
        correct_answer = html.unescape(data['correct_answer'])
        game_state = {
            'game_name': 'trivia',
            'state': {
                'question': question,
                'correct_answer': correct_answer.upper()
            }
        }
        active_games[user_id] = game_state
        logger.info(f"Starting Trivia game for {user_id}. Answer is {correct_answer}.")
        answers = [html.unescape(ans) for ans in data['incorrect_answers']] + [correct_answer]
        random.shuffle(answers)
        answer_text = "\n".join([f"• {ans}" for ans in answers])
        initial_message = (
            "Happy Birthday! :tada: Time for a little *Trivia*!\n\n"
            f"_{data['category']}_\n"
            f"*{question}*\n\n"
            f"{answer_text}\n\n"
            "Reply with your answer!"
        )
        await client.chat_postMessage(channel=user_id, text=initial_message)
    except Exception as e:
        logger.error(f"Failed to start Trivia game for {user_id} due to API/request error: {e}")
        await client.chat_postMessage(channel=user_id, text="Sorry, I couldn't get a trivia question right now. Maybe try again later!")

async def handle_trivia_guess(guess, user_id, game_session, say):
    game_state = game_session['state']
    correct_answer = game_state['correct_answer']
    if guess.strip().upper() == correct_answer:
        await say(f"That's correct! :star2: The answer was *{correct_answer}*. You're a trivia whiz!")
    else:
        await say(f"Sorry, that's not right. The correct answer was *{correct_answer}*.")
    del active_games[user_id]


# --- Game 4: Hangman ---
def _render_hangman_board(word, guessed_letters):
    display = ""
    for letter in word:
        if letter in guessed_letters:
            display += f"{letter} "
        else:
            display += "_ "
    return f"`{display.strip()}`"

async def start_hangman_game(user_id, client):
    target_word = random.choice(WORDLE_ANSWERS)
    game_state = {
        'game_name': 'hangman',
        'state': {
            'target': target_word,
            'guessed_letters': set(),
            'lives': 6
        }
    }
    active_games[user_id] = game_state
    logger.info(f"Starting Hangman game for {user_id}. Word is {target_word}.")
    board = _render_hangman_board(target_word, set())
    initial_message = (
        "Happy Birthday! :tada: Let's play a game of *Hangman*!\n\n"
        "I'm thinking of a 5-letter word.\n\n"
        f"{board}\n\n"
        f"You have *{game_state['state']['lives']}* lives left. Guess a letter!"
    )
    await client.chat_postMessage(channel=user_id, text=initial_message)

async def handle_hangman_guess(guess, user_id, game_session, say):
    game_state = game_session['state']
    target = game_state['target']
    if len(guess) == len(target) and guess == target:
        await say(f"You got it! The word was *{target}*. You win! :trophy:")
        del active_games[user_id]
        return
    if len(guess) != 1 or not guess.isalpha():
        await say("Please guess a single letter or the full word.")
        return
    if guess in game_state['guessed_letters']:
        await say(f"You already guessed '{guess}'. Try again!")
        return
    game_state['guessed_letters'].add(guess)
    if guess not in target:
        game_state['lives'] -= 1
        feedback = f"Sorry, no '{guess}'. You have *{game_state['lives']}* lives left."
    else:
        feedback = f"Good guess! '{guess}' is in the word."
    board = _render_hangman_board(target, game_state['guessed_letters'])
    if all(letter in game_state['guessed_letters'] for letter in target):
        await say(f"{board}\n\n{feedback}\n\nYou figured it out! The word was *{target}*. You win! :trophy:")
        del active_games[user_id]
        return
    if game_state['lives'] <= 0:
        await say(f"{board}\n\n{feedback}\n\nOh no, you're out of lives! The word was *{target}*. Better luck next time!")
        del active_games[user_id]
        return
    guessed_list = ", ".join(sorted(list(game_state['guessed_letters'])))
    await say(f"{board}\n\n{feedback}\n\n*Guessed letters:* {guessed_list}")


# --- Central Game Registry ---
GAME_REGISTRY = {
    "wordle": {
        "name": "Wordle",
        "start": start_wordle_game,
        "handler": handle_wordle_guess
    },
    "number_guesser": {
        "name": "Higher or Lower",
        "start": start_number_guesser_game,
        "handler": handle_number_guesser_guess
    },
    "trivia": {
        "name": "Trivia",
        "start": start_trivia_game,
        "handler": handle_trivia_guess
    },
    "hangman": {
        "name": "Hangman",
        "start": start_hangman_game,
        "handler": handle_hangman_guess
    }
}


# --- WORDLE HELPERS & GUI BUILDERS ---
# (The rest of the code is unchanged and can be copied from the previous version)
async def get_word_of_the_day():
    if not gemini_model:
        logger.warning("AI not configured. Using local fallback for Wordle answer.")
        return get_fallback_word()
    try:
        logger.info("Attempting to get Wordle answer from AI...")
        prompt = "choose a single, common, 5-letter English word suitable for a word game. Respond with only the single word and nothing else. word list: " + "\n".join(VALID_GUESSES)
        response = await gemini_model.generate_content_async(prompt)
        ai_word = response.text.strip().upper()
        if len(ai_word) == 5 and ai_word.isalpha() and ai_word in VALID_GUESSES:
            logger.info(f"AI chose a valid word of the day: {ai_word}")
            return ai_word
        else:
            logger.warning(f"AI provided an invalid word ('{ai_word}'). Using fallback.")
            return get_fallback_word()
    except Exception as e:
        logger.error(f"Error fetching word from AI: {e}. Using local fallback.")
        return get_fallback_word()
def get_fallback_word():
    random.seed(date.today().toordinal())
    return random.choice(WORDLE_ANSWERS)
def evaluate_guess(guess, target):
    results = [""] * 5; target_list = list(target); guess_list = list(guess)
    for i in range(5):
        if guess_list[i] == target_list[i]: results[i] = "🟩"; target_list[i] = None; guess_list[i] = None
    for i in range(5):
        if guess_list[i] is not None:
            if guess_list[i] in target_list: results[i] = "🟨"; target_list[target_list.index(guess_list[i])] = None
            else: results[i] = "⬛"
    return "".join(results)
async def is_real_word_with_ai(word):
    if not gemini_model:
        logger.warning("AI validation skipped: Gemini model not configured.")
        return False
    try:
        logger.info(f"Performing AI validation for word: {word}")
        prompt = f"Is '{word}' a real, common, 5-letter English word? Do not include proper nouns. Answer with only the single word 'yes' or 'no'."
        response = await gemini_model.generate_content_async(prompt)
        is_valid = response.text.strip().lower() == 'yes'
        if is_valid:
            logger.info(f"AI validation successful for '{word}'. Adding to dictionary.")
            with open("valid_guesses.txt", "a") as f: f.write(f"\n{word.lower()}")
            VALID_GUESSES.add(word)
        else:
             logger.info(f"AI validation rejected the word: {word}")
        return is_valid
    except Exception as e:
        logger.error(f"Error during AI word validation for '{word}': {e}")
        return False
async def generate_birthday_message(user_id):
    user_info = await get_user_info(slack_app.client, user_id); user_name = user_info['user']['profile'].get('real_name', 'our teammate') if user_info else 'our teammate'
    fallback_message = f"Happy Birthday <@{user_id}>! :tada:"
    if not gemini_model: logger.warning("Gemini model failed. Using fallback message."); return fallback_message
    try:
        logger.info(f"Generating Gemini birthday message for {user_name}...")
        prompt = (f"Generate fun, and enthusiastic birthday message for a colleague named {user_name}. The message should be posted in a company Slack channel. It must include a lot emojis. It must end by encouraging everyone to wish them a happy birthday at the end of the message. Make it exciting and celebratory. Do not use hashtags. Mention the user's name at least once.")
        response = await gemini_model.generate_content_async(prompt); logger.info("Gemini message generated successfully."); return response.text
    except Exception as e: logger.critical(f"CRITICAL ERROR during Gemini generation: {e}"); return fallback_message
async def generate_anniversary_message(user_id, years):
    user_info = await get_user_info(slack_app.client, user_id); user_name = user_info['user']['profile'].get('real_name', 'our teammate') if user_info else 'our teammate'
    fallback_message = f"Happy {years}-year anniversary, <@{user_id}>! :tada:"
    if not gemini_model: logger.warning("Gemini model failed. Using fallback message."); return fallback_message
    try:
        logger.info(f"Generating Gemini anniversary message for {user_name}...");
        prompt = (f"Generate a cheerful message for a colleague named {user_name} celebrating their *{years}-year* work anniversary. Make sure to prominently mention they are celebrating *{years} years*. Post it in a company Slack channel. It must include emojis. End by encouraging everyone to congratulate them. Make it sound appreciative. Do not use hashtags.")
        response = await gemini_model.generate_content_async(prompt); logger.info("Gemini message generated successfully."); return response.text
    except Exception as e: logger.critical(f"CRITICAL ERROR during Gemini generation: {e}"); return fallback_message
def build_settings_modal(callback_id, title, current_settings=None):
    channel = current_settings[1] if current_settings else None; time = current_settings[2] if current_settings else "09:00"
    channel_select_element = {"type": "channels_select", "placeholder": {"type": "plain_text", "text": "Select a channel"}, "action_id": "channel_select_action"}
    if channel: channel_select_element["initial_channel"] = channel
    return {"type": "modal", "callback_id": callback_id, "title": {"type": "plain_text", "text": title}, "submit": {"type": "plain_text", "text": "Save"}, "close": {"type": "plain_text", "text": "Cancel"}, "blocks": [{"type": "input", "block_id": "channel_block", "element": channel_select_element, "label": {"type": "plain_text", "text": "Where to post announcements?"}}, {"type": "input", "block_id": "time_block", "element": {"type": "timepicker", "initial_time": time, "action_id": "time_select_action"}, "label": {"type": "plain_text", "text": "What time should I post?"}}]}
def build_reset_modal(): return {"type": "modal", "callback_id": "reset_confirmed", "title": {"type": "plain_text", "text": "Confirm Bot Reset"}, "submit": {"type": "plain_text", "text": "Yes, Reset Now"}, "close": {"type": "plain_text", "text": "Cancel"}, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*:warning: DANGER: THIS CANNOT BE UNDONE. :warning:*"}}, {"type": "section", "text": {"type": "mrkdwn", "text": "This deletes *ALL* saved birthdays, anniversaries, and settings permanently."}}]}
def build_admin_set_birthday_modal(): return {"type": "modal", "callback_id": "admin_set_birthday_submitted", "title": {"type": "plain_text", "text": "Admin: Set Birthday"}, "submit": {"type": "plain_text", "text": "Save Birthday"}, "close": {"type": "plain_text", "text": "Cancel"}, "blocks": [{"type": "input", "block_id": "user_select_block", "label": {"type": "plain_text", "text": "Select a user"}, "element": {"type": "users_select", "placeholder": {"type": "plain_text", "text": "Select a user..."}, "action_id": "user_select_action"}}, {"type": "input", "block_id": "format_select_block", "label": {"type": "plain_text", "text": "Date Format"}, "element": {"type": "radio_buttons", "action_id": "format_select_action", "options": [{"text": {"type": "plain_text", "text": "MM-DD (e.g., 04-22)"}, "value": "MM-DD"}, {"text": {"type": "plain_text", "text": "DD-MM (e.g., 22-04)"}, "value": "DD-MM"}]}}, {"type": "input", "block_id": "date_input_block", "label": {"type": "plain_text", "text": "Enter Date"}, "element": {"type": "plain_text_input", "placeholder": {"type": "plain_text", "text": "e.g., 04-22"}, "action_id": "date_input_action"}}]}
def build_admin_set_anniversary_modal(): return {"type": "modal", "callback_id": "admin_set_anniversary_submitted", "title": {"type": "plain_text", "text": "Admin: Set Anniversary"}, "submit": {"type": "plain_text", "text": "Save Anniversary"}, "close": {"type": "plain_text", "text": "Cancel"}, "blocks": [{"type": "input", "block_id": "user_select_block", "label": {"type": "plain_text", "text": "Select a user"}, "element": {"type": "users_select", "placeholder": {"type": "plain_text", "text": "Select a user..."}, "action_id": "user_select_action"}}, {"type": "input", "block_id": "date_input_block", "label": {"type": "plain_text", "text": "Select their work start date"}, "element": {"type": "datepicker", "placeholder": {"type": "plain_text", "text": "Select a date"}, "action_id": "date_input_action"}}]}
def build_delete_type_modal(): return {"type": "modal", "callback_id": "delete_type_selected", "title": {"type": "plain_text", "text": "Delete Data"}, "submit": {"type": "plain_text", "text": "Next"}, "close": {"type": "plain_text", "text": "Cancel"}, "blocks": [{"type": "input", "block_id": "delete_type_block", "label": {"type": "plain_text", "text": "What do you want to delete?"}, "element": {"type": "radio_buttons", "action_id": "delete_type_action", "options": [{"text": {"type": "plain_text", "text": "A User's Birthday"}, "value": "birthday"}, {"text": {"type": "plain_text", "text": "A User's Anniversary"}, "value": "anniversary"}]}}]}
async def build_delete_user_modal(delete_type, client):
    title = f"Delete {delete_type.capitalize()}"; table_name = f"{delete_type}s"
    users_with_data = db_read_all(f"SELECT user_id FROM {table_name}")
    if not users_with_data: return None
    user_options = []
    for (user_id,) in users_with_data:
        info = await get_user_info(client, user_id)
        user_name = info['user']['real_name'] if (info and info['ok']) else user_id
        user_options.append({"text": {"type": "plain_text", "text": user_name}, "value": user_id})
    return {"type": "modal", "callback_id": f"delete_{delete_type}_confirmed", "title": {"type": "plain_text", "text": title}, "submit": {"type": "plain_text", "text": "Delete Forever"}, "close": {"type": "plain_text", "text": "Cancel"}, "blocks": [{"type": "input", "block_id": "user_select_block", "label": {"type": "plain_text", "text": "Select user to delete"}, "element": {"type": "static_select", "placeholder": {"type": "plain_text", "text": "Select a user..."}, "action_id": "user_select_action", "options": user_options}}]}
def build_game_settings_modal(current_status):
    options = [{"text": {"type": "plain_text", "text": "Enable Games"}, "value": "1"}, {"text": {"type": "plain_text", "text": "Disable Games"}, "value": "0"}]
    initial_option = next((opt for opt in options if opt["value"] == str(current_status)), None)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Enable this feature to send a random game to users on their birthday."}}, {"type": "input", "block_id": "game_status_block", "label": {"type": "plain_text", "text": "Birthday Game Status"}, "element": {"type": "radio_buttons", "action_id": "game_status_action", "options": options, **({"initial_option": initial_option} if initial_option else {})}}]
    return {"type": "modal", "callback_id": "game_settings_submitted", "title": {"type": "plain_text", "text": "Game Settings"}, "submit": {"type": "plain_text", "text": "Save"}, "close": {"type": "plain_text", "text": "Cancel"}, "blocks": blocks}
def build_test_game_modal():
    game_options = [
        {
            "text": {"type": "plain_text", "text": game_details["name"]},
            "value": game_key
        }
        for game_key, game_details in GAME_REGISTRY.items()
    ]
    return {
        "type": "modal",
        "callback_id": "test_game_selected",
        "title": {"type": "plain_text", "text": "Test a Game"},
        "submit": {"type": "plain_text", "text": "Start Test"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "game_select_block",
                "label": {"type": "plain_text", "text": "Which game would you like to test?"},
                "element": {
                    "type": "static_select",
                    "placeholder": {"type": "plain_text", "text": "Select a game"},
                    "action_id": "game_select_action",
                    "options": game_options
                }
            }
        ]
    }
# --- COMMAND HANDLERS, VIEW HANDLERS, EVENT HANDLERS, MESSAGE ROUTING ---
# ... and the rest of your file (which needs no further changes) ...
@slack_app.command("/help")
async def help_command(ack, body, client):
    await ack(); user_id = body['user_id']
    admin_command_text = "\n".join([
        "• `/setup-birthdays`: Configure birthday announcements.",
        "• `/setup-anniversary`: Configure anniversary announcements.",
        "• `/set-anniversary`: Set a user's work start date.",
        "• `/set-birthday`: Set a user's birthday.",
        "• `/set-game`: Enable or disable the birthday game.",
        "• `/list-birthdays`: List all saved birthdays.",
        "• `/list-anniversaries`: List all saved anniversaries.",
        "• `/test-birthday-ai`: Send a test birthday message.",
        "• `/test-anniversary-ai`: Send a test anniversary message.",
        "• `/test-game`: Starts a test game for yourself.",
        "• `/delete`: Delete a user's data.",
        "• `/reset-birthday-bot`: Reset all bot data and settings."
    ])
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Celebration Bot Help :wave:"}}, {"type": "section", "text": {"type": "mrkdwn", "text": "Here are the commands you can use:\n\n*User Commands:*\n• `/help`: Shows this help message."}}]
    if await is_user_admin(client, user_id): blocks.append({"type": "divider"}); blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Admin Commands:*\n{admin_command_text}"}})
    await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], blocks=blocks, text="Here are the available commands.")
@slack_app.command("/setup-birthdays")
async def setup_birthdays_command(ack, body, client):
    await ack();
    if not await is_user_admin(client, body['user_id']): await client.chat_postEphemeral(user=body['user_id'], channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    try: current_settings = db_read_one("SELECT * FROM settings_birthday WHERE id = 1"); view = build_settings_modal("birthday_settings_submitted", "Birthday Settings", current_settings); view['private_metadata'] = 'from_setup'; await client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e: logger.error(f"Error in /setup-birthdays: {e}")
@slack_app.command("/setup-anniversary")
async def setup_anniversary_command(ack, body, client):
    await ack()
    if not await is_user_admin(client, body['user_id']): await client.chat_postEphemeral(user=body['user_id'], channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    try: current_settings = db_read_one("SELECT * FROM settings_anniversary WHERE id = 1"); view = build_settings_modal("anniversary_settings_submitted", "Anniversary Settings", current_settings); await client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e: logger.error(f"Error in /setup-anniversary: {e}")
@slack_app.command("/set-game")
async def set_game_command(ack, body, client):
    await ack()
    if not await is_user_admin(client, body['user_id']): await client.chat_postEphemeral(user=body['user_id'], channel=body['channel_id'], text="Sorry, this is an admin-only command."); return
    try:
        current_setting = db_read_one("SELECT enabled FROM settings_game WHERE id = 1"); current_status = current_setting[0] if current_setting else 0
        view = build_game_settings_modal(current_status); await client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e: logger.error(f"Error in /set-game: {e}")
@slack_app.command("/reset-birthday-bot")
async def reset_command(ack, body, client):
    await ack()
    if not await is_user_admin(client, body['user_id']): await client.chat_postEphemeral(user=body['user_id'], channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    try: view = build_reset_modal(); await client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e: logger.error(f"Error in /reset-birthday-bot: {e}")
@slack_app.command("/set-birthday")
async def admin_set_birthday_command(ack, body, client):
    await ack()
    if not await is_user_admin(client, body['user_id']): await client.chat_postEphemeral(user=body['user_id'], channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    try: view = build_admin_set_birthday_modal(); await client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e: logger.error(f"Error opening admin set birthday modal: {e}")
@slack_app.command("/set-anniversary")
async def set_anniversary_command(ack, body, client):
    await ack()
    if not await is_user_admin(client, body['user_id']): await client.chat_postEphemeral(user=body['user_id'], channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    try: view = build_admin_set_anniversary_modal(); await client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e: logger.error(f"Error opening set anniversary modal: {e}")
@slack_app.command("/delete")
async def delete_data_command(ack, body, client):
    await ack()
    if not await is_user_admin(client, body['user_id']): await client.chat_postEphemeral(user=body['user_id'], channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    try: view = build_delete_type_modal(); await client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e: logger.error(f"Error opening delete data modal: {e}")
@slack_app.command("/list-birthdays")
async def list_birthdays_command(ack, body, client):
    await ack(); user_id = body['user_id']
    if not await is_user_admin(client, user_id): await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    all_birthdays = db_read_all("SELECT user_id, birthday_date FROM birthdays")
    if not all_birthdays: await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="No birthdays saved."); return
    today_tuple = (date.today().month, date.today().day)
    sorted_birthdays = sorted(all_birthdays, key=lambda b: (int(b[1][:2]), int(b[1][3:])) if (int(b[1][:2]), int(b[1][3:])) >= today_tuple else (int(b[1][:2]) + 12, int(b[1][3:])))
    message = ["*Upcoming Birthdays:*"]; [message.append(f"• <@{user_id}> - {datetime.strptime(bday_str, '%m-%d').strftime('%B %d')}") for user_id, bday_str in sorted_birthdays]
    await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="\n".join(message))
@slack_app.command("/list-anniversaries")
async def list_anniversaries_command(ack, body, client):
    await ack(); user_id = body['user_id']
    if not await is_user_admin(client, user_id): await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    all_anniversaries = db_read_all("SELECT user_id, anniversary_date FROM anniversaries")
    if not all_anniversaries: await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="No anniversaries saved."); return
    today = date.today(); today_tuple = (today.month, today.day)
    sorted_anniversaries = sorted(all_anniversaries, key=lambda a: (int(a[1][5:7]), int(a[1][8:10])) if (int(a[1][5:7]), int(a[1][8:10])) >= today_tuple else (int(a[1][5:7]) + 12, int(a[1][8:10])))
    message = ["*Upcoming Anniversaries:*"]
    for user_id, anniv_str in sorted_anniversaries:
        anniv_obj = datetime.strptime(anniv_str, "%Y-%m-%d").date(); years = relativedelta(today, anniv_obj).years
        if years >= 1: message.append(f"• <@{user_id}> - {anniv_obj.strftime('%B %d %Y')} ({years}-year anniversary)")
    if len(message) == 1: await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="No upcoming anniversaries for anyone who has been here at least a year.")
    else: await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="\n".join(message))
@slack_app.command("/test-birthday-ai")
async def test_birthday_ai_command(ack, body, client):
    await ack(); user_id = body['user_id']
    if not await is_user_admin(client, user_id): await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    settings = db_read_one("SELECT * FROM settings_birthday WHERE id = 1");
    if not settings: await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="Run `/setup-birthdays` first."); return
    _, channel, _ = settings
    await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text=f"Generating test AI birthday message... Posting in `#{await get_channel_name(client, channel)}`.")
    try: message = await generate_birthday_message(user_id); await client.chat_postMessage(channel=channel, text=message)
    except Exception as e: logger.error(f"Error in /test-birthday-ai: {e}"); await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text=f"Error: {e}")
@slack_app.command("/test-anniversary-ai")
async def test_anniversary_ai_command(ack, body, client):
    await ack(); user_id = body['user_id']
    if not await is_user_admin(client, user_id): await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="Sorry, You don't have the right permmision to do this action."); return
    settings = db_read_one("SELECT * FROM settings_anniversary WHERE id = 1");
    if not settings: await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="Run `/setup-anniversary` first."); return
    anniv_data = db_read_one("SELECT anniversary_date FROM anniversaries WHERE user_id = ?", (user_id,))
    if not anniv_data: await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="Cannot run test: Your anniversary date is not in the database."); return
    _, channel, _ = settings
    await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text=f"Simulating your real anniversary... Posting in `#{await get_channel_name(client, channel)}`.")
    try:
        anniv_date = datetime.strptime(anniv_data[0], "%Y-%m-%d").date(); years = relativedelta(date.today(), anniv_date).years
        if years == 0: await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="Cannot run test: Your start date is less than a year ago."); return
        message = await generate_anniversary_message(user_id, years); await client.chat_postMessage(channel=channel, text=message)
    except Exception as e: logger.error(f"Error in /test-anniversary-ai: {e}"); await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text=f"An error occurred: {e}")
@slack_app.command("/test-game")
async def test_game_command(ack, body, client):
    """MODIFIED: Opens a modal to let the admin choose which game to test."""
    await ack()
    user_id = body['user_id']
    if not await is_user_admin(client, user_id):
        await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="Sorry, this is an admin-only command.")
        return
    game_setting = db_read_one("SELECT enabled FROM settings_game WHERE id = 1")
    game_enabled = game_setting[0] if game_setting else 0
    if not game_enabled:
        await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text="The birthday games are currently disabled. Please enable them first using `/set-game`.")
        return
    try:
        view = build_test_game_modal()
        await client.views_open(trigger_id=body["trigger_id"], view=view)
    except Exception as e:
        logger.error(f"Error opening test game modal for {user_id}: {e}")
        await client.chat_postEphemeral(user=user_id, channel=body['channel_id'], text=f"An error occurred: {e}")
@slack_app.view("birthday_settings_submitted")
async def handle_birthday_settings_submission(ack, body, client, view):
    user_id = body["user"]["id"]; values = view["state"]["values"]
    try:
        channel, time = values["channel_block"]["channel_select_action"]["selected_channel"], values["time_block"]["time_select_action"]["selected_time"]
        db_write("INSERT OR REPLACE INTO settings_birthday (id, announcement_channel, announcement_time) VALUES (?, ?, ?)", (1, channel, time))
        channel_name = await get_channel_name(client, channel); confirmation_msg = f"Birthday settings saved! Announcements will be in `#{channel_name}` at `{time}`."
        await ack(); await client.chat_postMessage(channel=user_id, text=confirmation_msg); await update_scheduler()
        if view.get('private_metadata') == 'from_setup': await client.chat_postMessage(channel=user_id, text="Now collecting birthdays..."); await ask_for_all_birthdays(client)
    except SlackApiError as e:
        if e.response["error"] == "not_in_channel": await ack(response_action="errors", errors={"channel_block": "I can't post here. Please `/invite` me."})
        else: await ack(); await client.chat_postMessage(channel=user_id, text=f"An API error: {e}")
    except Exception as e: await ack(); logger.error(f"Error in birthday settings submission: {e}")
@slack_app.view("anniversary_settings_submitted")
async def handle_anniversary_settings_submission(ack, body, client, view):
    user_id = body["user"]["id"]; values = view["state"]["values"]
    try:
        channel, time = values["channel_block"]["channel_select_action"]["selected_channel"], values["time_block"]["time_select_action"]["selected_time"]
        db_write("INSERT OR REPLACE INTO settings_anniversary (id, announcement_channel, announcement_time) VALUES (?, ?, ?)", (1, channel, time))
        channel_name = await get_channel_name(client, channel); confirmation_msg = f"Anniversary settings saved! Announcements will be in `#{channel_name}` at `{time}`."
        await ack(); await client.chat_postMessage(channel=user_id, text=confirmation_msg); await update_scheduler()
    except SlackApiError as e:
        if e.response["error"] == "not_in_channel": await ack(response_action="errors", errors={"channel_block": "I can't post here. Please `/invite` me."})
        else: await ack(); await client.chat_postMessage(channel=user_id, text=f"An API error: {e}")
    except Exception as e: await ack(); logger.error(f"Error in anniversary settings submission: {e}")
@slack_app.view("game_settings_submitted")
async def handle_game_settings_submission(ack, body, client, view):
    user_id = body["user"]["id"]; values = view["state"]["values"]
    try:
        status = int(values["game_status_block"]["game_status_action"]["selected_option"]["value"])
        db_write("INSERT OR REPLACE INTO settings_game (id, enabled) VALUES (1, ?)", (status,))
        status_text = "enabled" if status == 1 else "disabled"; confirmation_msg = f"The Birthday Games have been *{status_text}*."
        await ack(); await client.chat_postMessage(channel=user_id, text=confirmation_msg)
    except Exception as e: logger.error(f"Error in game settings submission: {e}"); await ack(); await client.chat_postMessage(channel=user_id, text=f"An error occurred: {e}")
@slack_app.view("reset_confirmed")
async def handle_reset_confirmation(ack, body, client):
    user_id = body["user"]["id"]
    try: await ack(); db_reset(); await update_scheduler(); logger.info(f"Database reset by user {user_id}"); await client.chat_postMessage(channel=user_id, text="The Celebration Bot has been fully reset.")
    except Exception as e: logger.error(f"Error during reset confirmation: {e}")
@slack_app.view("admin_set_birthday_submitted")
async def handle_admin_set_birthday_submission(ack, body, client, view):
    admin_user_id = body["user"]["id"]; values = view["state"]["values"]
    try:
        target_user_id, selected_format, date_str = values["user_select_block"]["user_select_action"]["selected_user"], values["format_select_block"]["format_select_action"]["selected_option"]["value"], values["date_input_block"]["date_input_action"]["value"]
        format_string = "%m-%d" if selected_format == "MM-DD" else "%d-%m"
        parsed_date = datetime.strptime(date_str, format_string); db_date_str = parsed_date.strftime("%m-%d")
        db_write("INSERT OR REPLACE INTO birthdays (user_id, birthday_date) VALUES (?, ?)", (target_user_id, db_date_str))
        await ack(); await client.chat_postMessage(channel=admin_user_id, text=f"Success! Birthday for <@{target_user_id}> set to {parsed_date.strftime('%B %d')}.")
        try: await client.chat_postMessage(channel=target_user_id, text=f"FYI: An admin set your birthday to {parsed_date.strftime('%B %d')}.")
        except Exception: pass
    except ValueError: await ack(response_action="errors", errors={"date_input_block": "Invalid date for the selected format."})
    except Exception as e: logger.error(f"Error in admin birthday submission: {e}"); await ack(); await client.chat_postMessage(channel=admin_user_id, text=f"An unexpected error: {e}")
@slack_app.view("admin_set_anniversary_submitted")
async def handle_admin_set_anniversary_submission(ack, body, client, view):
    admin_user_id = body["user"]["id"]; values = view["state"]["values"]
    try:
        target_user_id, date_str = values["user_select_block"]["user_select_action"]["selected_user"], values["date_input_block"]["date_input_action"]["selected_date"]
        if not date_str: await ack(response_action="errors", errors={"date_input_block": "A date must be selected."}); return
        db_write("INSERT OR REPLACE INTO anniversaries (user_id, anniversary_date) VALUES (?, ?)", (target_user_id, date_str))
        await ack(); await client.chat_postMessage(channel=admin_user_id, text=f"Success! Anniversary for <@{target_user_id}> set to {date_str}.")
        try: await client.chat_postMessage(channel=target_user_id, text=f"FYI: An admin has set your work anniversary date to {date_str}.")
        except Exception: pass
    except Exception as e: logger.error(f"Error in admin anniversary submission: {e}"); await ack(); await client.chat_postMessage(channel=admin_user_id, text=f"An unexpected error: {e}")
@slack_app.view("delete_type_selected")
async def handle_delete_type_selection(ack, body, client):
    admin_user_id = body['user']['id']
    delete_type = body['view']['state']['values']['delete_type_block']['delete_type_action']['selected_option']['value']
    view_to_open = await build_delete_user_modal(delete_type, client)
    if view_to_open is None: await ack(); await client.chat_postEphemeral(user=admin_user_id, channel=admin_user_id, text=f"No {delete_type}s to delete."); return
    await ack(response_action="update", view=view_to_open)
@slack_app.view("delete_birthday_confirmed")
async def handle_delete_birthday(ack, body, client):
    admin_user_id = body['user']['id']; user_to_delete = body['view']['state']['values']['user_select_block']['user_select_action']['selected_option']['value']
    db_write("DELETE FROM birthdays WHERE user_id = ?", (user_to_delete,))
    await ack(); await client.chat_postMessage(channel=admin_user_id, text=f"Deleted birthday for <@{user_to_delete}>.")
@slack_app.view("delete_anniversary_confirmed")
async def handle_delete_anniversary(ack, body, client):
    admin_user_id = body['user']['id']; user_to_delete = body['view']['state']['values']['user_select_block']['user_select_action']['selected_option']['value']
    db_write("DELETE FROM anniversaries WHERE user_id = ?", (user_to_delete,))
    await ack(); await client.chat_postMessage(channel=admin_user_id, text=f"Deleted anniversary for <@{user_to_delete}>.")
@slack_app.view("test_game_selected")
async def handle_test_game_selection(ack, body, client):
    """NEW: Handles the submission of the 'Test a Game' modal."""
    await ack()
    user_id = body["user"]["id"]
    values = body["view"]["state"]["values"]
    selected_game_key = values["game_select_block"]["game_select_action"]["selected_option"]["value"]
    start_function = GAME_REGISTRY[selected_game_key]["start"]
    game_name = GAME_REGISTRY[selected_game_key]["name"]
    try:
        await client.chat_postEphemeral(
            user=user_id,
            channel=user_id, # Post in DMs
            text=f"Starting a test of the *{game_name}* game in your DMs..."
        )
        await start_function(user_id, client)
    except Exception as e:
        logger.error(f"Failed to start test game '{game_name}' for {user_id}: {e}")
        await client.chat_postEphemeral(user=user_id, channel=user_id, text="Sorry, an error occurred while trying to start that game.")
@slack_app.event("team_join")
async def handle_team_join(event, client):
    new_user_id = event["user"]["id"]; logger.info(f"New user joined: {new_user_id}")
    try:
        format_str, example_str = await get_user_date_format(client, new_user_id)
        await client.chat_postMessage(channel=new_user_id, text=f"<@{new_user_id}>, Welcome to the team! I'm the Birthday Bot. To make sure we can celebrate you, please reply to me with your birthday in `{format_str}` format ({example_str}).")
        all_users = await client.users_list(); admin_reminder_message = f":wave: A new user named <@{new_user_id}>, has joined! Please remember to set their work anniversary using the `/set-anniversary` command."
        for user in all_users['members']:
            if await is_user_admin(client, user['id']) and not user['is_bot']:
                try: await client.chat_postMessage(channel=user['id'], text=admin_reminder_message)
                except Exception as e: logger.error(f"Failed to send admin reminder to {user['id']}: {e}")
    except Exception as e: logger.error(f"Error in team_join event for {new_user_id}: {e}")
@slack_app.event("user_change")
async def handle_user_change(event, client):
    user_profile = event["user"]
    if user_profile.get("deleted", False):
        user_id = user_profile["id"]; logger.info(f"User {user_id} has been deactivated. Removing their data.")
        db_write("DELETE FROM birthdays WHERE user_id = ?", (user_id,)); db_write("DELETE FROM anniversaries WHERE user_id = ?", (user_id,))
        admin_reminder_message = f"User, <@{user_id}>, has been deleted and their data has been removed."
        all_users = await client.users_list()
        for user in all_users['members']:
            if await is_user_admin(client, user['id']) and not user['is_bot']:
                try: await client.chat_postMessage(channel=user['id'], text=admin_reminder_message)
                except Exception as e: logger.error(f"Failed to send admin reminder to {user['id']}: {e}")
@slack_app.message()
async def handle_dm(message, say, client):
    """MODIFIED: Handles all DMs and routes game guesses to the correct handler."""
    if message.get("channel_type") != "im": return
    user_id = message["user"]
    text = message["text"].strip() # Standardize input a bit
    # --- Game Logic Routing ---
    if user_id in active_games:
        game_session = active_games[user_id]
        game_key = game_session['game_name']
        # Look up the correct handler in the registry and call it
        handler_function = GAME_REGISTRY[game_key]['handler']
        await handler_function(text.upper(), user_id, game_session, say) # Pass standardized text
        return
    # --- Birthday Submission Logic (no change) ---
    birthday_match = re.fullmatch(r"(\d{2}-\d{2})", text)
    if birthday_match:
        date_str = birthday_match.group(1)
        if db_read_one("SELECT 1 FROM birthdays WHERE user_id = ?", (user_id,)):
            await say("I already have your birthday saved! Ask an admin to update it if it's incorrect.")
            return
        try:
            date_format, _ = await get_user_date_format(client, user_id)
            format_string = "%d-%m" if date_format == 'DD-MM' else "%m-%d"
            parsed_date = datetime.strptime(date_str, format_string)
            db_date_str = parsed_date.strftime("%m-%d")
            db_write("INSERT INTO birthdays (user_id, birthday_date) VALUES (?, ?)", (user_id, db_date_str))
            await say(f"Got it! I'll celebrate your birthday on {parsed_date.strftime('%B %d')}!")
        except ValueError:
            await say("That date is invalid or in the wrong format. Please try again.")
        except Exception as e:
            logger.error(f"Error saving birthday for {user_id}: {e}")
        return
async def ask_for_all_birthdays(client):
    try:
        result = await client.users_list()
        for user in result["members"]:
            if not user["is_bot"] and user["id"] != "USLACKBOT" and not db_read_one("SELECT 1 FROM birthdays WHERE user_id = ?", (user['id'],)):
                try:
                    format_str, example_str = await get_user_date_format(client, user['id'])
                    response = await client.conversations_open(users=user["id"])
                    await client.chat_postMessage(channel=response["channel"]["id"], text=f"Hi! I'm the new Celebration Bot. To get started, please reply with your birthday in `{format_str}` format ({example_str}).")
                except Exception as e: logger.error(f"Error sending DM to {user['id']}: {e}")
    except Exception as e: logger.error(f"Error fetching users: {e}")
async def daily_birthday_check():
    """MODIFIED: Randomly chooses a game to start for birthday users."""
    settings = db_read_one("SELECT * FROM settings_birthday WHERE id = 1")
    if not settings: return
    _, channel, _ = settings
    today_str = date.today().strftime("%m-%d")
    birthdays_today = db_read_all("SELECT user_id FROM birthdays WHERE birthday_date = ?", (today_str,))
    game_setting = db_read_one("SELECT enabled FROM settings_game WHERE id = 1")
    game_enabled = game_setting[0] if game_setting else 0
    for (user_id,) in birthdays_today:
        try:
            # Post the birthday announcement first
            message = await generate_birthday_message(user_id)
            await slack_app.client.chat_postMessage(channel=channel, text=message)
            # If games are enabled, pick one at random and start it
            if game_enabled:
                game_keys = list(GAME_REGISTRY.keys())
                chosen_game_key = random.choice(game_keys)
                logger.info(f"Randomly selected game '{chosen_game_key}' for birthday user {user_id}")
                # Look up the start function in the registry and call it
                start_function = GAME_REGISTRY[chosen_game_key]['start']
                await start_function(user_id, slack_app.client)
        except Exception as e:
            logger.critical(f"CRITICAL ERROR during daily_birthday_check for {user_id}: {e}")
async def daily_anniversary_check():
    settings = db_read_one("SELECT * FROM settings_anniversary WHERE id = 1")
    if not settings: return
    _, channel, _ = settings
    today_str = date.today().strftime("%m-%d")
    anniversaries_today = db_read_all("SELECT user_id, anniversary_date FROM anniversaries WHERE SUBSTR(anniversary_date, 6) = ?", (today_str,))
    for user_id, anniv_str in anniversaries_today:
        try:
            anniv_date = datetime.strptime(anniv_str, "%Y-%m-%d").date()
            years = relativedelta(date.today(), anniv_date).years
            if years > 0:
                message = await generate_anniversary_message(user_id, years)
                await slack_app.client.chat_postMessage(channel=channel, text=message)
        except Exception as e: logger.critical(f"CRITICAL ERROR during daily_anniversary_check for {user_id}: {e}")
async def update_scheduler():
    bday_settings = db_read_one("SELECT announcement_time FROM settings_birthday WHERE id = 1")
    anniv_settings = db_read_one("SELECT announcement_time FROM settings_anniversary WHERE id = 1")
    bday_job = scheduler.get_job('daily_bday_check')
    anniv_job = scheduler.get_job('daily_anniv_check')

    if bday_settings:
        hour, minute = map(int, bday_settings[0].split(':'))
        if bday_job: scheduler.reschedule_job('daily_bday_check', trigger='cron', hour=hour, minute=minute)
        else: scheduler.add_job(daily_birthday_check, 'cron', hour=hour, minute=minute, id='daily_bday_check')
        logger.info(f"Birthday scheduler set for {hour:02d}:{minute:02d} daily.")
    elif bday_job:
        scheduler.remove_job('daily_bday_check'); logger.info("Birthday settings not found. Scheduler stopped.")
    if anniv_settings:
        hour, minute = map(int, anniv_settings[0].split(':'))
        if anniv_job: scheduler.reschedule_job('daily_anniv_check', trigger='cron', hour=hour, minute=minute)
        else: scheduler.add_job(daily_anniversary_check, 'cron', hour=hour, minute=minute, id='daily_anniv_check')
        logger.info(f"Anniversary scheduler set for {hour:02d}:{minute:02d} daily.")
    elif anniv_job:
        scheduler.remove_job('daily_anniv_check'); logger.info("Anniversary settings not found. Scheduler stopped.")
@api_app.on_event("startup")
async def startup_event():
    logger.info("Starting up...")
    setup_database()
    scheduler.start()
    await update_scheduler()
    logger.info("Startup complete.")
@api_app.on_event("shutdown")
def shutdown_event():
    logger.info("Shutting down...")
    scheduler.shutdown()
    logger.info("Shutdown complete.")
@api_app.post("/slack/events")
async def slack_events_endpoint(req: Request):
    """This single endpoint handles all incoming events from Slack."""
    return await slack_handler.handle(req)