# Phase 3 Task 0: no branded content on TikTok private posts (`feat/phase3` → `main`)

In `PUBLISH_MODE=private`, TikTok posts are now sent as `SELF_ONLY` with every branded-content flag off. A new guard
refuses to send any private TikTok payload that is not `SELF_ONLY` or that has a branded field. Live mode is
unchanged.

**This deliberately changes compliance invariant 4 and its tests.** Carson requested it in the Phase 3 Task 0
instructions. The exact changes are listed below. No other invariant changed. `schemas.py` is unchanged.

**Approval needed:** COMPLIANCE.md requires Carson's explicit approval, in this PR, for a change that relaxes an
invariant. Carson: please confirm by comment or review before merging. Nobody else can give it.

## Why

The Phase 1 D open question in ARCHITECTURE §4.7 is now answered from the official docs (checked 2026-11-23):

- **TikTok** (https://developers.tiktok.com/docs/en/content-sharing-guidelines, last updated 2026-08-04): branded
  content "can only be configured with visibility as public/friends". If visibility is "only me", the Branded Content
  option must be disabled, or visibility switched to public.
- **Upload-Post** (https://docs.upload-post.com/api/upload-video and
  https://docs.upload-post.com/guides/paid-partnership):
  - `paid_partnership=true` with `privacy_level=SELF_ONLY` is refused with HTTP 400
    `commercial_disclosure_privacy_conflict`.
  - `disclose_commercial=true` + `branded_content=true` with `SELF_ONLY` is **published as `PUBLIC_TO_EVERYONE`**.
  - A `brand_content_toggle` that we send, even as `false`, always overrides `paid_partnership`.

So with the old payload (`SELF_ONLY` + `brand_content_toggle=true`), a private test would either fail or risk going
out publicly. A `SELF_ONLY` post is visible only to the operator, so leaving the label off exposes no viewer to an
undisclosed ad.

## Behaviour

| Mode | TikTok `privacy_level` | `brand_content_toggle` | Other branded fields | Caption |
| --- | --- | --- | --- | --- |
| live, dry_run | `PUBLIC_TO_EVERYONE` (unchanged) | `true` (unchanged) | none sent (unchanged) | `#ad ` first |
| private | `SELF_ONLY` | `false` (was `true`) | none: no `paid_partnership`, `disclose_commercial`, `branded_content`, `your_brand` | `#ad ` first |

`brand_organic_toggle=false` and `is_aigc=false` in every mode, as before. YouTube private keeps
`hasPaidProductPlacement=true`. Instagram, X and YouTube are untouched.

**Guard:** `compliance.assert_tiktok_disclosure(payload, private=...)` raises `ComplianceError` (invariant 4). It runs
twice:
- at the end of `build_payload`;
- in `UploadPostClient.publish` on the final form-encoded values, just before the HTTP call.

Keys are compared at any depth in a compact form: lowercase, with `_`, `-`, spaces and `[]` removed. So
`paid_partnership`, `paidPartnership` and `PAID-PARTNERSHIP` all count as the same key.

In private mode it raises if:
- any key ending in `privacylevel` (`privacy_level`, `privacyLevel`, `tiktok_privacy_level`, ...) is anything other
  than exactly the string `SELF_ONLY`, or `privacy_level` is missing (a missing level means the account default,
  which can be public);
- `brand_content_toggle` or `brand_organic_toggle` is missing, or any key ending in `brandcontenttoggle` or
  `brandorganictoggle` is not false;
- any key contains `paidpartnership`, `disclosecommercial`, `brandedcontent` or `yourbrand`, with any value, even
  false.

Outside private mode it raises unless every privacy-level key is exactly `PUBLIC_TO_EVERYONE` (a missing one also
raises) and every brand-content toggle is true. This is a new check that makes invariant 4 stricter for live
posts.

The dispatcher already catches `ComplianceError` and fails the post locally ("refused before sending"), so no HTTP
call is made.

## Exact compliance changes (invariant 4)

**`docs/COMPLIANCE.md`**
- Invariant 4 row:
  - added the private-mode TikTok exception (`SELF_ONLY`, `brand_content_toggle=false`, `brand_organic_toggle=false`,
    no `paid_partnership` / `disclose_commercial` / `branded_content` / `your_brand` in any form);
  - added that outside private mode TikTok is always `PUBLIC_TO_EVERYONE` and `brand_content_toggle=true`;
  - added that a payload breaking either rule raises before anything is sent;
  - added `compliance.assert_tiktok_disclosure` to "Enforced in" and the new tests to "Test".
- New "Note 4" paragraph:
  - the reason for the exception, with the TikTok and Upload-Post URLs;
  - the exception covers only the branded flags: `#ad` (invariant 2), the "Ad" label (invariant 3), AI flags false,
    and YouTube private `hasPaidProductPlacement=true` all still apply;
  - a private TikTok post must never be switched to public by hand.

**`tests/compliance/test_inv04_disclosure_flags.py`**
- `test_disclosure_flags_per_platform`:
  - now parametrised over `DRY_RUN` and `LIVE` (it was `DRY_RUN` only);
  - the TikTok check is stricter: `brand_content_toggle is True` (it was a truthy check), plus
    `privacy_level == "PUBLIC_TO_EVERYONE"`;
  - all other assertions are unchanged;
  - removed a stale `# type: ignore[attr-defined]` on the import.
- Unchanged: `test_no_synthetic_media_libraries_in_src`.
- New tests:
  - `test_tiktok_private_is_self_only_and_never_branded`: private mode is `SELF_ONLY`, both toggles `False`,
    `is_aigc` `False`, no branded key, caption and title start with `#ad `. Run with 4 caption variants.
  - `test_tiktok_caption_starts_with_ad_in_every_mode`: live, private and dry_run, with and without a `#ad` in the
    input.
  - `test_guard_accepts_the_good_private_payload_as_dict_and_form`.
  - `test_guard_raises_on_bad_private_payload`, 31 cases:
    - privacy level: each non-`SELF_ONLY` value, lowercase `self_only`, a missing level, `["SELF_ONLY"]`,
      `tiktok_privacy_level`, `privacyLevel`;
    - toggles: `brand_content_toggle` set to `True`, `"true"`, `"1"` or `None`; `brand_organic_toggle="yes"`;
      `tiktok_brand_content_toggle="true"`; `brandContentToggle="true"`; `tiktok_brand_organic_toggle=True`;
    - forbidden keys: `paid_partnership` (`True` and `False`), `PAID-PARTNERSHIP`, `paidPartnership`,
      `tiktok_paid_partnership`, `x_paid_partnership`; `disclose_commercial` (`True` and `False`),
      `discloseCommercial`; `branded_content`, `brandedContent`, `is_branded_content`; `your_brand`, `yourBrand`;
    - a nested `paid_partnership`.
  - `test_guard_raises_when_private_toggle_is_missing`: either toggle left out.
  - `test_guard_raises_on_unbranded_or_private_live_payload`, 12 cases:
    - brand toggle false, `"false"` or `None`, `tiktok_brand_content_toggle="false"`, `brandContentToggle=False`;
    - privacy level `SELF_ONLY`, `MUTUAL_FOLLOW_FRIENDS`, `FOLLOWER_OF_CREATOR`, missing, or `["SELF_ONLY"]`;
    - `tiktok_privacy_level` or `privacyLevel` set to `SELF_ONLY`.
  - `test_guard_accepts_the_good_live_payload`.
  - `test_youtube_private_keeps_paid_product_placement`: shows the exception is TikTok-only (YouTube private keeps
    `hasPaidProductPlacement=true`, `containsSyntheticMedia=false` and `#ad`).
  - `test_bad_private_payload_is_never_sent`: `build_payload` is bypassed with a bad payload; `publish` raises and
    respx records 0 calls.
  - `test_private_request_body_has_no_branded_field`: the real multipart body has `SELF_ONLY` and
    `brand_content_toggle=false`, and none of the four forbidden fragments.

**Changed outside `tests/compliance/`**
- `tests/publish/test_upload_post.py::test_private_mode_values`: TikTok private `brand_content_toggle` is now
  `False` (it was `True`, with the comment "invariant 4 holds in every mode"). Added `"paid_partnership" not in tt`.
  The YouTube private assertions are unchanged.
- `tests/publish/test_dispatcher.py::test_private_mode_sends_tiktok_only_as_self_only`: added assertions that the
  sent body has `brand_content_toggle=false` and no `paid_partnership`. Nothing was removed.

No test was deleted, skipped or xfailed.

## Other files

- `src/clipforge/compliance.py`: the guard, its constants, and the verified facts in the module docstring.
- `src/clipforge/publish/upload_post.py`: `brand_content_toggle` is now `not private`; the guard is called in
  `build_payload` and `publish`; the docstring has the verified facts.
- `docs/ARCHITECTURE.md` §4.7: the TikTok field list now shows the live/private split. The open question is replaced
  with the verified facts and URLs. Other things the docs say: unaudited API clients can only post `SELF_ONLY`,
  omitting `privacy_level` uses the account default, and an unavailable level is refused with
  `tiktok_privacy_unavailable`.

## Verification

- `make lint test`: ruff clean; 1122 passed, 11 skipped, 1 deselected.
- `mypy --strict src/clipforge/schemas.py src/clipforge/compliance.py`: no issues.
- compliance-reviewer subagent, first pass: one FAIL. The behaviour was correct, but the guard's key matching was too
  narrow: camelCase or prefixed keys such as `paidPartnership`, `tiktok_brand_content_toggle` and `privacyLevel`
  slipped through. The live check also accepted levels other than `PUBLIC_TO_EVERYONE`, or a missing level. It also
  asked for the YouTube-private check under `tests/compliance/` and for Carson's approval in this PR. The first
  three are fixed in this branch, with tests. The approval is requested above.
- compliance-reviewer, second pass: **PASS** for invariant 4 and for invariants 1–12 and the "also enforced" rules,
  with 219 compliance tests passing. 28 hand-made bypass payloads were tried (camelCase, spaced, prefixed, nested,
  list-valued keys and values); every bad one raised and the good ones were accepted. Merge still needs Carson's
  approval (see above).

## Follow-up (not in this PR)

The reviewer noted that private mode skips only Instagram (`dispatcher.py`). If X were enabled, a private run would
post publicly to X, because X has no private mode. X is off by default, so this is not a live risk today. It should
be handled before X is turned on.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
