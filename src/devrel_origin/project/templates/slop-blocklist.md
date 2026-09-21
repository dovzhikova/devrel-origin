# Tier 1 words adapted from petergyang/no-ai-slop (MIT). Context-dependent words
# and structural patterns are judged, not matched; see quality/questions.py.

# Anti-slop blocklist

# Words, phrases, and patterns that mark text as AI-written. The quality pipeline rewrites any content that contains a hit; on second failure it aborts loud with a report listing offenders.

# One entry per line. Lines starting with `#` are comments and ignored. Matching is case-insensitive against word boundaries.

## Hedge words and filler
perhaps
furthermore
moreover
in conclusion
in today's
in this fast-paced world

## AI tells
delve
delves
tapestry
seamless
seamlessly
unleash
unleashing
revolutionize
revolutionary
empower
empowering
groundbreaking
foster
leverage
utilize
facilitate
streamline
robust
cutting-edge
paradigm shift
game changer
realm
beacon
multifaceted
meticulous
intricate
paramount
transformative
elevate
embark
supercharge
harness
ever-evolving

## Generic CTAs
learn more
discover more
get started today
contact us today

## Listicle filler
in this article we will
this article will explore
in this post

## Empty intensifiers
truly
incredibly
extremely
very
really
