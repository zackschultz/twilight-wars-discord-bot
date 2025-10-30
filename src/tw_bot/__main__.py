import os

from .bot import run


if __name__ == "__main__":
    run(
        application_id=int(os.environ["BOT_APPLICATION_ID"]),
        application_secret=os.environ["BOT_APPLICATION_SECRET"],
    )
