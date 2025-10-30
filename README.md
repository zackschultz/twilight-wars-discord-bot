# twilight-wars-bot

## Env setup

```bash
# cd your/project/root
python3.14 -m venv ./venv
source ./venv/bin/activate
pip install pip-tools
echo "$(pwd)/src" > ./venv/lib/python3.14/site-packages/project_src.pth
pip install -r requirements-dev.txt
```

## Virtualenv Activation

- Mac: `source ./venv/bin/activate`

## Compiling/Pinning Dependencies

Python dependencies must be declared in pyproject.toml. The two requirements files are pinned and must be regenerated from pyproject as needed.

```bash
pip-compile --output-file requirements.txt pyproject.toml
pip-compile --extra dev --output-file requirements-dev.txt pyproject.toml
# Good idea to re-install the dependencies after pinning
pip install -r requirements-dev.txt
```

## Running the bot

You will need to [create a Discord bot account](https://discordpy.readthedocs.io/en/stable/discord.html) for your bot. Once you have credentials, create a text file named `.env` in the project root. It should look like:

```
BOT_APPLICATION_ID=<your Discord application id>
BOT_APPLICATION_SECRET=<your Discord application secret>
```

### Running the bot locally

After activating your virtual environment, run the following command:

```bash
env $(cat .env | xargs) python -m tw_bot
```

This will load the relevant configuration into the environment and kick off bot.

### Running the bot in Docker

Navigate to the project root directory.

To build the docker image, use

```bash
docker compose build
```

Once the image has been built, you can run the bot in the background

```bash
docker compose up --detach
```

Docker compose will read the API tokens from the .env file and pass them into the container as appropriate.

### Inviting your bot and connecting it to a game

Invite your bot to a channel you want to use for notifications. Once there, use the `/connectgame` command. You should enter the URL for your game (e.g. https://www.twilightwars.com/games/123abc), your username and your password. The bot will use your username/password to generate a session token, but will not save them.

Once the bot is connected, each player must use `/connectplayer` to set themselves to receive @-mentions when it is their turn. Each Twilight Wars player can mention one discord user.
