"""Weibo post analysis question set. Instructions/criteria in English; post text stays Chinese.

Adapted from tools/jev/questions.py for public weibo posts instead of 1:1 chat.
Question types follow tools/jev/TASK.md: noul returns {"noul": 0..1}, choice returns
{"choice", "probabilities", "confidence"}, score returns {"score", "legend",
"probabilities", "confidence"} with an ordered criteria array of concrete scenes.

The state shape for weibo posts is:
    {"post": {"author", "text", "retweeted_text"?, "context"?}}
"""

from __future__ import annotations

WEIBO_QUESTIONS: dict = {
    "post_intent": {
        "type": "choice",
        "instructions": (
            "What is the poster's true intent for this weibo post? "
            "Look at the whole post, including any retweeted content. "
            "Prefer tone and framing over surface wording. "
            "A marketing post disguised as a personal share is promote_ad. "
            "A rage-bait framing of a public event meant to gather sides is drum_up_support, "
            "even if it looks like an opinion. "
            "An obviously fake or sensational unverified news repost is spread_unverified, not share_opinion."
        ),
        "criteria": {
            "share_opinion": (
                "They want to state and spread their own view on a topic, "
                "expecting agreement or discussion. The claim is presented as their take."
            ),
            "express_emotion": (
                "Mainly venting or celebrating personal feelings: anger, sadness, joy, pride. "
                "No ask, no argument being sought."
            ),
            "promote_ad": (
                "Promoting a product, service, own account, group, or link. "
                "Commercial or self-promotion motive, possibly disguised as experience sharing."
            ),
            "drum_up_support": (
                "Framing an event or dispute to incite sides, rally anger, and gather "
                "agreement or retweets. The heat is the point, not the facts."
            ),
            "spread_unverified": (
                "Broadcasting unverified or sensational news as if it were true; "
                "the main point is dissemination, not their own opinion."
            ),
            "seek_help": (
                "Asking for practical help, advice, or information from readers."
            ),
            "troll_attack": (
                "Deliberately provoking or insulting a person or group to start a fight. "
                "Not a sincere opinion."
            ),
            "casual_share": (
                "Light daily-life sharing with no agenda: meals, pets, travel, hobbies."
            ),
        },
    },
    "danger_level": {
        "type": "score",
        "instructions": (
            "How harmful is this post if readers take it at face value? "
            "Score the content's risk, not the poster's style. "
            "Match the current scene to the most concrete bin. "
            "If the post only vents a feeling about no named target, stay low. "
            "If it names real people with unverified accusations, it is at least bin 5. "
            "Scam or hate content jumps straight to bin 7 or above."
        ),
        "criteria": [
            "Harmless daily share: food, travel, pets, jokes. No claim about other people or events.",
            "Mild subjective opinion or personal emotion with no named target and no factual claim.",
            "Opinionated commentary on public topics without heat; no insults, no unverified factual claims.",
            "One-sided narrative about a controversy; blame is implied but no private person is named; a few unverified details told as fact.",
            "Unverified claims ('someone said', forwarded screenshots) or exaggerated framing presented without a source.",
            "Named person or group accused on unverified material, or strong leading language meant to inflame readers.",
            "Clear defamation, doxxing hints, or calls for others to attack a named person; or scam-style promos.",
            "Selling scams or fraud (paid rides, fake followers, money-recovery tricks), hate speech against groups, or active doxxing.",
            "Fraud instructions, phishing links, malware, threats, or incitement to offline violence.",
            "Clearly illegal: terror or hate incitement, child exploitation, death threats, organized-crime recruitment.",
        ],
    },
    "credibility": {
        "type": "choice",
        "instructions": (
            "How credible are the post's factual claims, if any? "
            "Judge the factual assertions inside the post, not whether you like the opinion. "
            "If the post makes no checkable factual claim, choose opinion_not_factual. "
            "If claims rest only on 'netizens say' or forwarded screenshots with no source, "
            "choose plausible_unverified unless the framing distorts known facts, which is misleading_exaggeration. "
            "If the claim contradicts established public facts, choose clear_falsehood."
        ),
        "criteria": {
            "verified_fact": (
                "Factual claims consistent with public records and verifiable from known sources, "
                "or first-hand reports with concrete details."
            ),
            "plausible_unverified": (
                "Sounds plausible but no source is given; cannot be confirmed from the post alone."
            ),
            "misleading_exaggeration": (
                "Mixes truth with distortion: cherry-picked, exaggerated, or out-of-context framing."
            ),
            "clear_falsehood": (
                "Contradicts established facts or is an obvious fabrication."
            ),
            "opinion_not_factual": (
                "No checkable factual claim: pure feeling, taste, or clearly-labeled speculation."
            ),
        },
    },
    "aggression": {
        "type": "score",
        "instructions": (
            "How aggressive or attacking is this post? "
            "Score the language level from polite to violent. "
            "Sarcasm about an idea is bin 3; sarcasm aimed at a named person is bin 4. "
            "Calls for others to attack, or doxxing-adjacent content, is bin 7 or above "
            "regardless of how calmly it is worded."
        ),
        "criteria": [
            "Calm, polite, or warm. No target.",
            "Mild disagreement stated respectfully.",
            "Confident disagreement without insults; heat aimed at ideas, not people.",
            "Sarcasm about an idea or event, with no named person.",
            "Sarcasm or mockery aimed at a named person or group.",
            "Direct insults or cursing at a named target.",
            "Insults plus collective blame of a whole group (fans, gender, region).",
            "Calls for others to attack, harass, or cancel a target.",
            "Beyond words: leaking private info, doxxing, organizing offline action.",
            "Explicit threats of violence or illegal acts.",
        ],
    },
    "should_engage": {
        "type": "noul",
        "instructions": (
            "Should you engage with substantive content? "
            "Substantive means: adding facts you actually know, offering real help, "
            "or asking a question that can be answered. "
            "This is NOT 'do you agree' and NOT 'do you feel like replying'. "
            "Answer FALSE if the poster thrives on heat (troll or rage bait), "
            "if replying would spread unverified claims further, if the matter is already settled, "
            "or if the substance you would add is not actually known to you (you would be guessing). "
            "Answer true only if a substantive and safe reply is available from the facts given."
        ),
        "criteria": {
            "true": (
                "You know the facts or the real help the post asks for, and a substantive "
                "reply is safe: it does not feed a fight or amplify unverified claims."
            ),
            "false": (
                "Do not engage with substance: the poster wants heat rather than truth, "
                "replying would spread unverified content, the topic is settled, "
                "or the needed facts are not known and saying less is safer."
            ),
        },
    },
    "best_action": {
        "type": "choice",
        "instructions": (
            "What type of action is best for this post? "
            "Choose the action type only; do not decide timing. "
            "If the post is unverified but not hostile, politely_question or fact_check_refute "
            "beats a confident correction. "
            "Use correct_with_source only when you actually know the correct information "
            "and can name a verifiable source. "
            "Choose report when the content crosses a line (scam, hate, doxxing, explicit violence)."
        ),
        "criteria": {
            "agree_support": (
                "Genuinely agree or support; add useful same-side facts, or comfort a sincere emotion."
            ),
            "politely_question": (
                "Ask a calm clarifying question. The premise is unclear but goodwill is possible."
            ),
            "fact_check_refute": (
                "Point out the claim looks wrong and why, without asserting the correction as fact."
            ),
            "correct_with_source": (
                "State the correct information with a verifiable source. Only when you actually know it."
            ),
            "ignore": (
                "Best not to engage. Replying feeds the fire or wastes everyone's time."
            ),
            "report": (
                "Content crosses a line: scam, hate, doxxing, threats. Report or block instead of arguing."
            ),
        },
    },
    "poster_needs": {
        "type": "choice",
        "instructions": (
            "What does the poster need from readers right now? "
            "A sincere seek_help post needs help. "
            "A rage-bait or troll post needs engagement_traffic — the argument itself is the product. "
            "A promo aimed at strangers, or a concluded discussion, needs nothing from you. "
            "An unverified news repost usually needs dissemination more than agreement."
        ),
        "criteria": {
            "attention": (
                "Wants to be seen and heard: likes, replies, comfort, sympathy."
            ),
            "dissemination": (
                "Wants the post spread wider: retweets, shares, more reach."
            ),
            "agreement": (
                "Wants their view validated; looking for supporters."
            ),
            "help": (
                "Needs practical help, information, or advice."
            ),
            "engagement_traffic": (
                "Wants heat and traffic; an argument is the product; sincerity is not the point."
            ),
            "nothing": (
                "Needs nothing from you: a promo to strangers, an already-concluded discussion, "
                "or pure aesthetic sharing."
            ),
        },
    },
}


def build_state(author: str, text: str, retweeted_text: str = "", context: str = "") -> dict:
    """Build the state dict for a weibo post. Empty optional fields are omitted."""
    post: dict = {"author": author or "", "text": text}
    if retweeted_text:
        post["retweeted_text"] = retweeted_text
    if context:
        post["context"] = context
    return {"post": post}


def build_rank_question(candidates: list[str]) -> dict:
    """Build the best_reply choice question. criteria values stay in original Chinese."""
    if len(candidates) != 3:
        raise ValueError("build_rank_question expects exactly 3 candidate replies")
    keys = ("reply_a", "reply_b", "reply_c")
    return {
        "best_reply": {
            "type": "choice",
            "instructions": (
                "Which candidate reply is the most appropriate public reply to this post, "
                "given the poster's true intent, the post's credibility, and its risk? "
                "Prefer replies that add known facts, ask clarifying questions, or de-escalate. "
                "Penalize replies that spread unverified claims, pour fuel on a fight, "
                "over-trust a suspicious promo, or are off-topic."
            ),
            "criteria": {key: text for key, text in zip(keys, candidates)},
        }
    }