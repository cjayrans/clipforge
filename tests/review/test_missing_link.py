"""A post the platform published but has not given a link for yet: the SUBMIT NOW notice, reminders, /posturl.

TikTok releases the public post ID after moderation (hours). Content Rewards wants the link within the submission
window, counted from publishing, so the operator copies it from the app and gives it to the bot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from clipforge.config import Settings, load_settings
from clipforge.db import EventRow, PostRow, SubmissionRow, make_session_factory, transition
from clipforge.publish.manual_url import PostUrlError, attach_post_url, validate_post_url
from clipforge.review import bot, keyboards
from clipforge.review.bot import BotDeps
from clipforge.review.evening import PRIVATE_WARNING, send_reminders, send_submission_notices
from clipforge.schemas import Platform, PostState, SubmissionState
from tests.review.fakes import FakeNotifier, callback_data, make_context, make_update
from tests.support.factories import Factory

OWNER = 111
PUBLISHED = datetime(2026, 11, 24, 18, 12, 50, tzinfo=UTC)  # 13:12:50 ET
LINK = "https://www.tiktok.com/@gaming/video/7571234567890123456"


@pytest.fixture
def private_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("PUBLISH_MODE", "private")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake-token-for-tests")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", str(OWNER))
    return load_settings()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def published_without_link(factory: Factory, session: Session, *, published: datetime | None = PUBLISHED) -> PostRow:
    """The state the poller leaves a post in when Upload-Post says completed but gave no URL."""
    clip = factory.clip(
        candidate=factory.candidate(captions_json={"tiktok": "#ad Terra: a question for the scientist"})
    )
    post = factory.post(clip, Platform.TIKTOK, upload_post_job="1", request_id="1")
    transition(session, post, PostState.PUBLISHING)
    post.published_at = published
    if published is not None:
        factory.submission(post, deadline_at=published + timedelta(minutes=30))
    session.commit()
    return post


async def test_notice_without_a_url_says_how_to_get_and_send_the_link(
    factory: Factory, session: Session, private_settings: Settings, notifier: FakeNotifier
) -> None:
    post = published_without_link(factory, session)
    assert await send_submission_notices(session, private_settings, notifier, PUBLISHED + timedelta(seconds=40)) == 1
    assert await send_submission_notices(session, private_settings, notifier, PUBLISHED + timedelta(minutes=1)) == 0
    (sent,) = notifier.sent
    text = sent.text
    assert text.startswith("SUBMIT NOW: LINK NOT AVAILABLE YET")
    assert "has not released its link yet" in text
    assert "Copy link" in text  # tells the operator to copy it from the TikTok app
    assert f"/posturl {post.id} <the link you copied>" in text  # and exactly how to hand it over
    assert 'caption starts "#ad Terra: a question for the scientist"' in text
    assert "Submit here: https://contentrewards.com/c/test-" in text
    # The deadline is 30 minutes after the PUBLISH time (13:12 ET), not after the notice or the link.
    assert "Deadline: 13:42, which is 30 min after it was published at 13:12." in text
    assert "count from the publish time, not from when you get the link" in text
    assert PRIVATE_WARNING in text  # private mode: a SELF_ONLY post must not be submitted
    assert [keyboards.decode(d)[0] for d in callback_data(sent.keyboard)] == ["sub", "mis"]


async def test_non_private_notice_has_no_private_warning(
    factory: Factory, session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    assert settings.publish_mode.value == "dry_run"  # the default; no test ever sets live
    published_without_link(factory, session)
    await send_submission_notices(session, settings, notifier, PUBLISHED)
    assert PRIVATE_WARNING not in notifier.texts[0]
    assert "/posturl" in notifier.texts[0]


async def test_a_post_not_known_to_be_published_gets_no_notice(
    factory: Factory, session: Session, private_settings: Settings, notifier: FakeNotifier
) -> None:
    published_without_link(factory, session, published=None)  # still processing at Upload-Post: no submission yet
    assert await send_submission_notices(session, private_settings, notifier, PUBLISHED) == 0
    assert notifier.sent == []


async def test_reminder_repeats_the_missing_link_and_the_command(
    factory: Factory, session: Session, private_settings: Settings, notifier: FakeNotifier
) -> None:
    post = published_without_link(factory, session)
    await send_submission_notices(session, private_settings, notifier, PUBLISHED)
    assert await send_reminders(session, private_settings, notifier, PUBLISHED + timedelta(minutes=19)) == 0
    assert await send_reminders(session, private_settings, notifier, PUBLISHED + timedelta(minutes=20)) == 1
    assert await send_reminders(session, private_settings, notifier, PUBLISHED + timedelta(minutes=21)) == 0
    reminder = notifier.sent[-1].text
    assert reminder.startswith("Reminder:")
    assert "10 min left, and the link is still missing" in reminder
    assert f"/posturl {post.id}" in reminder


async def test_notice_with_a_url_in_private_mode_warns_not_to_submit(
    factory: Factory, session: Session, private_settings: Settings, notifier: FakeNotifier
) -> None:
    post = published_without_link(factory, session)
    attach_post_url(session, private_settings, post.id, LINK, PUBLISHED + timedelta(minutes=5))
    session.commit()
    await send_submission_notices(session, private_settings, notifier, PUBLISHED + timedelta(minutes=5))
    assert notifier.texts[0].startswith("SUBMIT NOW\n" + PRIVATE_WARNING)
    assert f"Post: {LINK}" in notifier.texts[0]


# --- /posturl ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@gaming/video/7571234567890123456",
        "https://vm.tiktok.com/ZMabc123/",
        "https://www.tiktok.com/@gaming/video/75?is_from_webapp=1&sender_device=pc",
    ],
)
def test_tiktok_links_are_accepted(url: str) -> None:
    assert validate_post_url(Platform.TIKTOK, url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://www.tiktok.com/@gaming/video/1",  # not https
        "https://www.youtube.com/watch?v=abc",  # wrong platform
        "https://tiktok.com.evil.example/@a/video/1",  # look-alike host
        "https://nottiktok.com/@a/video/1",
        "https://user:pw@www.tiktok.com/@a/video/1",
        "https://www.tiktok.com/",  # the site, not the post
        "https://www.tiktok.com/@a/video/1 and more",
        "not a link",
    ],
)
def test_bad_links_are_refused(url: str) -> None:
    with pytest.raises(PostUrlError):
        validate_post_url(Platform.TIKTOK, url)


def test_attach_makes_the_post_live_with_the_operator_as_actor(
    factory: Factory, session: Session, private_settings: Settings
) -> None:
    post = published_without_link(factory, session)
    attach_post_url(session, private_settings, post.id, LINK, PUBLISHED + timedelta(minutes=9))
    session.commit()
    assert post.state is PostState.LIVE
    assert post.post_url == LINK
    assert post.published_at == PUBLISHED  # the publish time is kept
    (sub,) = session.scalars(select(SubmissionRow)).all()
    assert sub.deadline_at == PUBLISHED + timedelta(minutes=30)
    assert sub.state is SubmissionState.AWAITING
    event = session.scalars(
        select(EventRow).where(
            EventRow.entity == "post", EventRow.from_state == "publishing", EventRow.to_state == "live"
        )
    ).one()
    assert event.actor == "operator"
    assert "link given by the operator" in event.detail


@pytest.mark.parametrize("state", ["live", "failed", "unpublished"])
def test_attach_refuses_posts_in_the_wrong_state(
    factory: Factory, session: Session, private_settings: Settings, state: str
) -> None:
    post = published_without_link(factory, session, published=None if state == "unpublished" else PUBLISHED)
    if state == "live":
        attach_post_url(session, private_settings, post.id, LINK, PUBLISHED)
    elif state == "failed":
        transition(session, post, PostState.FAILED)
    with pytest.raises(PostUrlError) as err:
        attach_post_url(session, private_settings, post.id, LINK.replace("7571234567890123456", "1"), PUBLISHED)
    if state == "failed":
        assert "cf post repair" in str(err.value)
    if state == "live":
        assert post.post_url == LINK  # the first link was not overwritten


def test_attach_unknown_post(session: Session, private_settings: Settings) -> None:
    with pytest.raises(PostUrlError, match="no post 999"):
        attach_post_url(session, private_settings, 999, LINK, PUBLISHED)


@pytest.fixture
def deps(private_settings: Settings, engine: Engine, notifier: FakeNotifier) -> BotDeps:
    return BotDeps(
        settings=private_settings,
        factory=make_session_factory(engine),
        notifier=notifier,
        clock=lambda: PUBLISHED + timedelta(minutes=5),
    )


async def _send(deps: BotDeps, chat_id: int, *args: str) -> str:
    update = make_update(chat_id)
    ctx = make_context(deps)
    ctx.args = list(args)
    await bot.cmd_posturl(update, ctx)
    reply = update.effective_message.reply_text
    return str(reply.await_args.args[0]) if reply.await_args is not None else ""


async def test_posturl_command_saves_the_link(factory: Factory, session: Session, deps: BotDeps) -> None:
    post = published_without_link(factory, session)
    reply = await _send(deps, OWNER, str(post.id), LINK)
    assert f"Post {post.id} (tiktok): link saved" in reply
    assert "Deadline: 13:42" in reply
    session.expire_all()
    assert session.get(PostRow, post.id).post_url == LINK  # type: ignore[union-attr]


async def test_posturl_command_refusals_are_explained_and_change_nothing(
    factory: Factory, session: Session, deps: BotDeps
) -> None:
    post = published_without_link(factory, session)
    assert "/posturl <post id> <link>" in await _send(deps, OWNER)
    assert "/posturl <post id> <link>" in await _send(deps, OWNER, "abc", LINK)
    assert "not a tiktok link" in await _send(deps, OWNER, str(post.id), "https://www.youtube.com/watch?v=1")
    assert "no post 4242" in await _send(deps, OWNER, "4242", LINK)
    session.expire_all()
    assert session.get(PostRow, post.id).post_url is None  # type: ignore[union-attr]


async def test_posturl_command_is_owner_only(factory: Factory, session: Session, deps: BotDeps) -> None:
    post = published_without_link(factory, session)
    assert await _send(deps, 222, str(post.id), LINK) == ""  # silently ignored for anyone else
    session.expire_all()
    assert session.get(PostRow, post.id).post_url is None  # type: ignore[union-attr]


def test_posturl_is_registered_and_listed_in_help() -> None:
    from clipforge.review.bot import register_handlers

    class Recorder:
        def __init__(self) -> None:
            self.commands: set[str] = set()

        def add_handler(self, handler: object, group: int = 0) -> None:
            self.commands |= set(getattr(handler, "commands", ()))

    rec = Recorder()
    register_handlers(rec)  # type: ignore[arg-type]
    assert "posturl" in rec.commands


def test_attach_reports_a_race_with_the_poller(
    factory: Factory, session: Session, private_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforge.db.models import IllegalTransition
    from clipforge.publish import manual_url

    def lost_race(*args: object, **kwargs: object) -> None:
        raise IllegalTransition("PostRow 1 is already live")

    monkeypatch.setattr(manual_url, "_go_live", lost_race)
    post = published_without_link(factory, session)
    with pytest.raises(PostUrlError, match="changed while saving"):
        attach_post_url(session, private_settings, post.id, LINK, PUBLISHED)
