# ScreenWatch seating intelligence: research dossier

Status: research synthesis plus implemented optimization contract
Research date: 2026-08-11
Scope: reserved-seat cinemas; individual and group recommendations; conventional rows, recliners, loveseats/sofas, pods, premium formats, accessibility, and partially occupied auditoriums.

## Executive conclusion

There is no defensible universal pattern such as “always sit two-thirds back,” “keep every group in one row,” or “split 15 people 5+5+5.” The optimal result is a constrained, personalized allocation over two graphs:

1. a **seat graph**, whose edges encode physical adjacency, same-row adjacency, cross-row proximity, aisle/module boundaries, accessibility links, and obstructions; and
2. a **party graph**, whose weighted edges encode which people most need or want to sit together (date/partner, parent-child or caregiver, close friends, coworkers, and weak/unknown ties).

The system should first eliminate illegal or unsuitable allocations, then protect the worst-served member of the party, then preserve the most important social ties, then maximize overall viewing/comfort quality. Venue inventory concerns such as avoiding orphan seats belong only among the final tie-breakers unless the ticketing system makes them hard constraints.

The algorithm should produce a small Pareto frontier instead of pretending that one hidden scalar captures every preference: for example, **Best overall**, **Stay together**, **Best view**, and **Easy access**. Personal history can learn how the user trades these objectives, but learned preference must not override accessibility or child/caregiver constraints.

## Implemented optimization contract

ScreenWatch now implements the research as a two-regime solver rather than claiming that one heuristic is always exact:

1. **Exact regime:** when the complete combinatorial space is tractable, enumerate every seat subset satisfying the party's hard constraints. The returned certificate records the method, number of combinations considered, and a zero optimality gap.
2. **Anytime regime:** in large rooms, generate bounded row-run, module-respecting, and multi-row candidates. The certificate explicitly says the answer is not proven globally optimal and gives a valid upper bound on remaining normalized robust-utility regret. The product must never relabel this as an exact answer.
3. **Uncertain geometry:** convert source-level geometry confidence into a distribution-free interval for every seat's viewing utility. Fairness and robustness use the conservative end of that interval; precise venue coordinates collapse the interval to the point score.
4. **Fair social welfare:** separately maximize the worst person's utility (an egalitarian/Rawlsian guardrail) and the geometric mean of everyone’s utility (Nash social welfare). This avoids both “fourteen great seats and one awful seat” and an unnecessarily inefficient allocation that improves the worst seat by a negligible amount.
5. **Preference robustness:** score each feasible arrangement under sightline-first, connection-first, egalitarian, and low-regret operational profiles. The default maximizes the worst result across those profiles instead of relying on one fragile weight vector. This follows the motivation of relative robust multicriteria decision-making under unknown preferences ([Karimi et al., *Management Science*, 2025](https://pubsonline.informs.org/doi/10.1287/mnsc.2025.00510)).
6. **Pareto safety and diversity:** discard a candidate only when another is no worse in every modeled dimension and better in at least one. Alternatives are selected for structural distance as well as merit, following the broader diverse near-optimal solution literature ([DiversiTree](https://pubsonline.informs.org/doi/10.1287/ijoc.2022.0164)), so the top results are not five cosmetic seat shifts.

The certificate proves optimality only **inside the declared mathematical model and known seat map**. It does not prove that ScreenWatch has measured every human preference or every physical property of the auditorium. That distinction is permanent product policy.

## Evidence method

Sources were separated by evidentiary role rather than pooled indiscriminately:

- **A — direct/current:** current cinema engineering guidance, law, official ticketing schemas, or peer-reviewed research directly involving reserved-seat venues.
- **B — adjacent peer-reviewed:** vision, acoustics, proxemics, social-choice, or allocation research that transfers plausibly but was not conducted specifically for cinema groups.
- **C — operational:** cinema/vendor documentation describing actual seat types and booking rules.
- **D — community:** repeated first-person reports from moviegoers and cinema staff. Useful for discovering preferences and failure modes; not sufficient to establish a universal optimum.
- **E — inference:** a proposed product rule derived from multiple sources. These rules require validation with ScreenWatch outcomes.

Community evidence was deliberately sampled across general cinemas, IMAX, Dolby Cinema, 4DX, loveseat/sofa layouts, solo attendance, dates, families, large groups, aisle preferences, stranger proximity, accessibility, and orphan-seat rules. Contradictory reports were retained.

## Corrections to popular rules

### “SMPTE says 30 degrees is optimal” — not a current normative rule

The frequently cited SMPTE EG 18-1994 document is explicitly [withdrawn](https://pub.smpte.org/pub/eg18/eg0018-1994_withdrawn2003.pdf) and says its content is no longer endorsed. It remains useful historical context, but it cannot be presented as a current authoritative optimum.

Current public guidance is better treated as a feasible comfort region. The [European Digital Cinema Forum 2019 guide](https://www.edcf.net/edcf_docs/EDCF_ABestPracticesGuide_Architecture_ViewingConditions.pdf) considers under 80° horizontal field of view comfortable for the first row on a 2.39:1 screen, recommends under 45° off-axis to screen center (under 35° is “good”), and recommends a vertical angle under 25° to screen center. [THX](https://www.thx.com/questions/thx-certified-screen-placement/) uses 36° at the farthest seat as an auditorium-design target and calls for clear sightlines. These are design boundaries, not proof that every viewer prefers exactly one angle.

**Product consequence (A/E):** estimate horizontal field of view, vertical elevation, and off-axis distortion when screen geometry is known. When it is not, use calibrated row/column proxies with uncertainty. Score a broad comfort/immersion band, not a single magic row.

### “Two-thirds back is the audio sweet spot” — too simplistic

[ISO 2969:2015](https://www.iso.org/standard/43646.html), confirmed current in 2023, concerns consistent cinema sound response across installations and listening positions. [SMPTE RP 2096-1:2017](https://pub.smpte.org/latest/rp2096-1/rp2096-1-2017.pdf) calibrates cinema sound over a listening area using multiple microphone positions and final listening. [Dolby Atmos cinema specifications](https://professional.dolby.com/siteassets/cinema/dolby-audio-products/dolby-atmos-specifications.pdf) define a reference listening position but aim speakers across a critical listening area; the published geometry does not justify treating one seat as uniquely correct.

**Product consequence (A/E):** centerline remains a strong default, but depth should be a format- and auditorium-specific band. ScreenWatch should learn venue-specific rows from repeat choices/feedback instead of labeling two-thirds back as objective fact.

### “Subtitle viewers should always sit farther back” — plausible, weakly quantified

The EDCF guide notes that sitting too close can make it difficult to read subtitles without head movement. Eye-tracking work shows that bottom subtitles alter gaze behavior around film cuts ([Loschky et al.](https://pmc.ncbi.nlm.nih.gov/articles/PMC10723748/)), and subtitle/image switching creates cognitive load ([Kruger et al.](https://www.mdpi.com/2414-4088/8/6/51)). Neither establishes an exact cinema row shift.

**Product consequence (B/E):** “subtitles” may gently penalize extreme field of view and large vertical scanning, but should not impose a hard farther-back rule. Validate the effect through user feedback.

## What the evidence strongly supports

### Geometry is continuous; seat maps are not rectangular truth

The EDCF guide documents independent penalties for excessive field of view, lateral off-axis viewing, vertical elevation, seating beyond screen edges, screen-gain effects, and sightline clearance. Large-screen formats change those relationships. Community reports repeatedly identify venue-specific anomalies: a numerically “center” IMAX seat may not align with screen center; an overhang can impair rear sound; recliners can make a formerly uncomfortable front row tolerable; 4DX effects vary by row and by four-seat motion module ([IMAX venue discussion](https://www.reddit.com/r/imax/comments/133ato3/best_seats_for_certain_imax_theaters/), [4DX discussion](https://www.reddit.com/r/RegalUnlimited/comments/1eq2qfg/best_seat_for_4dx/)).

**Required representation (A/C/D/E):** retain coordinates, row depth, row spans, gaps, aisles, sections, seat orientation if known, screen geometry if known, and format-specific modules. Do not infer adjacency merely because seat numbers are consecutive.

### Personal-space preference is real, heterogeneous, and occupancy-dependent

The peer-reviewed [Locational Choices](https://journals.sagepub.com/doi/10.1177/0022243720941525) research models reserved-seat decisions using screen/aisle position and nearby occupancy. Its experiments show substantial individual heterogeneity and find that forced-choice purchase logs understate the desire to avoid nearby strangers. The authors’ working paper reports roughly 50% top-choice prediction from a few observations when chance was about 5–7% ([full working paper](https://thearf-org-unified-admin.s3.amazonaws.com/MSI/2020/06/MSI_Report_18-128-1.pdf)). [RecSeats](https://doi.org/10.1145/3383313.3412263) further shows that seat preference depends on neighboring availability and that a hybrid individual-choice/CNN model can outperform either component alone.

Community reports show the boundary condition: sitting immediately beside a stranger in a nearly empty auditorium is widely described as awkward, while adjacency in a crowded prime zone is generally accepted ([empty-versus-crowded discussion](https://www.reddit.com/r/cineplex/comments/1r353dp/is_it_bad_etiquette_to_buy_a_seat_right_beside_a/)).

**Product consequence (A/D/E):** stranger proximity is a personalized, occupancy-aware penalty. It should decay rapidly with distance, become weaker as occupancy rises or alternatives disappear, and never override a major viewing-quality loss without evidence that the user wants that trade.

### Physical modules are first-class constraints

[Vista’s seating documentation](https://developer.vista.co/digital-platform/seating) distinguishes normal, sofa, wheelchair, and companion seats; sofas can have left/middle/right positions and can require whole-module purchase. [MovieXchange](https://apidocs.moviexchange.com/docs/seating) explicitly returns `seatsInGroup` for sofa and wheelchair/companion groups. Amazon’s ticketing schema likewise exposes `loveseatleft` and `loveseatright` ([Ticketing SPI](https://www.developer.amazon.com/docs/alexaplus/add-ons/ticketing-spi.html)). Community reports disagree on whether sharing a divided loveseat module with a stranger is harmless or uncomfortable, confirming that the boundary has social meaning but not identical meaning to everyone ([sofa discussion](https://www.reddit.com/r/RegalUnlimited/comments/1p0wjtp/is_it_weird_to_sit_next_to_strangers_on_the_sofa/)).

**Product consequence (C/D/E):** parsers must preserve module identity and side, not collapse everything to a generic “loveseat” kind. A couple/date should receive a complete two-person module when possible. For 3 or 5 people, the algorithm should compare one full module plus a neighboring single/other module against splitting into conventional seats; it must not cut a required-purchase module or silently pair one party member with a stranger.

### Accessibility is a hard feasibility layer, not a seat-quality bonus

The [U.S. Department of Justice ticket-sales guidance](https://www.ada.gov/resources/ticket-sales/) permits an eligible purchaser to obtain up to three contiguous companion seats with an accessible space when available and requires comparable nearby alternatives when not. The [2010 ADA standards guidance](https://www.ada.gov/law-and-regs/design-standards/standards-guidance/) requires substantially equivalent choices of viewing locations and angles. Accessible features may be needed by people who do not use wheelchairs. Ticketmaster models wheelchair, limited-mobility, sight/hearing, and companion inventory separately and does not allow a companion seat to be purchased alone in its documented ADA flow ([Ticketmaster partner API](https://developer.ticketmaster.com/products-and-docs/apis/partner/ada/)).

**Product consequence (A/C/E):** an accessibility requirement changes the feasible set. Never recommend an accessible space as generic overflow, never strand the eligible person from a required companion/caregiver, and never assume all accessibility needs are visible or equivalent. Aisle/easy-egress requests should be explicit preferences or requirements.

### Group compactness and group fairness are distinct objectives

Operations research has long represented group allocation as candidate blocks plus exact optimization. An IBM airline study used [set packing and promising candidate generation](https://doi.org/10.15807/jorsj.42.32) to keep group members near one another with practical computation. A congress allocation model minimizes shortest-path distance to each party’s central seat and adds connectivity/fairness constraints ([Bach et al.](https://doi.org/10.1007/s11750-019-00515-3)). Modern seat-arrangement research distinguishes utilitarian welfare (sum of utilities) from egalitarian welfare (minimum individual utility), and shows that these problems are generally NP-hard even on restricted seat graphs ([Ceylan, Chen, and Roy](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4701385)). [Balanced and Fair Partitioning of Friends](https://doi.org/10.1609/aaai.v39i13.33503) formalizes why maximizing retained friendship edges alone is not enough: a partition can have a good total while leaving one person with none of their important ties.

**Product consequence (B/E):** use a lexicographic fairness objective: first protect the minimum person-level experience and mandatory social ties, then optimize the total. “Compact” should be measured with graph distances and social-edge cuts, not just bounding-box area.

### Orphan-seat avoidance belongs to the venue, not to the definition of human optimality

The live-entertainment allocation literature notes that free seat choice strands singles and models group-size-aware inventory allocation for revenue/capacity ([Maclean and Ødegaard](https://doi.org/10.1016/j.ejor.2020.02.012)). Real cinemas enforce no-single-gap rules ([Prince Charles Cinema policy](https://princecharlescinema.com/faq/); [Cineworld user reports](https://www.reddit.com/r/CineworldUnlimited/comments/1rug67a/anyone_else_find_the_please_choose_new_seats/)). These rules preserve future sellable pairs, but users also report being blocked from a preferred seat in mostly empty screenings.

**Product consequence (A/C/D/E):** if the vendor rejects orphan gaps, treat the rule as hard. Otherwise use expected inventory damage only as a late tie-breaker, scaled by occupancy and time-to-show. ScreenWatch’s primary duty is to its user.

## Proposed formal problem

### Inputs

- Party members `P`, not merely party size.
- Weighted party graph `G_p`: mandatory adjacency, preferred adjacency, supervision/caregiver edges, relationship strength, and individual requirements/preferences.
- Seat graph `G_s`: available seats plus typed edges for same-row neighbor, cross-row near, same physical module, same section, aisle crossing, and walking distance.
- Auditorium geometry: screen bounds/center, row depth/elevation, seat coordinates/orientation, obstructions/overhangs, screen type when known.
- Showtime context: format (2D, 3D, IMAX geometry class, Dolby, 4DX/ScreenX/D-BOX), occupancy, booking rules, time to show, and prices.
- User model: learned center/depth/immersion/aisle/stranger/recliner preferences with confidence and recency.

### Hard constraints

1. Every selected seat is available and purchasable.
2. Exact party cardinality is met, accounting for spaces versus physical chairs.
3. Sofa/pod/motion modules obey vendor purchase and occupancy rules.
4. Accessibility, companion, caregiver, and child-supervision requirements are satisfied.
5. Vendor-enforced orphan-gap, section, ticket-type, and maximum-order rules are satisfied.
6. The proposal remains valid under ambiguous map parsing; uncertain adjacency cannot be treated as certain.

### Lexicographic objective

Among feasible allocations, compare solutions in this order:

1. **Safety and dignity:** no violated accessibility/supervision/module rule.
2. **Worst-person utility:** maximize the minimum individual experience score.
3. **Critical social continuity:** minimize cuts of mandatory and high-weight party edges; forbid isolating a person when a non-isolating feasible alternative is reasonably close in seat quality.
4. **Split burden:** minimize number of components, then maximum graph distance between components, then row span/aisle crossings.
5. **Total human utility:** viewing geometry, acoustics proxy, comfort, individual preferences, and stranger proximity.
6. **Robustness:** prefer solutions that remain good under uncertain screen center, row order, module boundaries, and future nearby bookings.
7. **Cost and venue externalities:** price if requested, then orphan inventory and fragmentation as tie-breakers.

This ordering prevents a high total score from sacrificing one child, companion, date, or edge member of a large group.

### Candidate generation and solving

1. Normalize the map into a typed graph; identify row runs, physical modules, sections, aisles, and uncertain edges.
2. Generate high-quality connected blocks around multiple possible medoids, not only around the geometric center.
3. Generate multi-component shapes with bounded row span: same row; two adjacent rows; three adjacent rows; aligned/staggered blocks; whole-module combinations; accessibility-centered combinations.
4. For each seat subset, solve the people-to-seat assignment separately using the party graph. A good geometric subset can still be a bad social arrangement.
5. Apply dominance pruning: discard a candidate only if another is no worse on every objective and strictly better on at least one.
6. Solve the remaining small candidate set exactly (branch-and-bound, integer programming, or dynamic programming by row/module). Use a time budget and retain the best certified incumbent.
7. Return diverse Pareto options with plain-language reasons and warnings. Never hide a split.

The graph/candidate/exact-solver pattern is preferable to a monolithic neural model. It is explainable, testable, and handles novel auditorium topology. A learned model is valuable for estimating personal utility or ranking Pareto candidates, where RecSeats-style evidence applies.

## Scenario implications

These are policies for candidate generation and comparison, not hard-coded answers.

### 1 person

Optimize personal geometry and access. Apply an occupancy-aware stranger penalty. Do not use accessible/companion inventory without an eligible requirement. Avoid creating an orphan only when enforced or when the viewing loss is negligible.

### Date or close pair

Mandatory same-component and usually direct same-row adjacency. Prefer a complete loveseat module when available and desired. Compare the pair’s two individual seat utilities, not just the midpoint, so “centered” does not place one person materially off-axis. Respect aisle/side preferences.

### Two friends or coworkers

Direct adjacency remains the default, but loveseat intimacy should not be assumed. Prefer independent seats or a clearly divided module unless history or an explicit preference says otherwise.

### 3 people in two-seat modules

Never pretend a 2+1 split is socially neutral. Compare, in order: three conventional adjacent seats; a full two-seat module plus a directly neighboring single with no stranger-sharing; two nearby modules if one seat can legally remain unused/purchased; then a visible 2+1 split. Put the person with the strongest combined ties or supervision role at the boundary/bridge only if that improves everyone’s minimum connectivity; rotate/ask when ties are equal rather than always isolating the “third.”

### 4 people

Compare four contiguous conventional seats, two adjacent loveseat modules, and 2x2 across adjacent rows. A 2x2 block can dominate a four-seat line when it improves everyone’s viewing angle and preserves two strong dyads, but it can be worse for a single four-person friend clique.

### 5 people

Compare 5 contiguous, 3+2 across adjacent rows, 4+1 only when the singleton remains directly near and socially appropriate, and whole-module compositions such as 2+2+1. A 3+2 shape normally beats 4+1 on worst-person social utility. Parent/child or couple edges determine the internal assignment.

### 6–10 people

One long row is not automatically best. Generate balanced adjacent-row partitions (for example 4+4, 5+4, 5+5), stagger them around a shared center, and compare them with a contiguous row. Penalize extreme edge members, aisle crossings, and components that cannot see/hear one another before the film. Preserve couples/caregiver ties inside components while distributing highly connected “bridge” people across row boundaries.

### 11–20 people, including 15 and 20

Treat the task as compact graph partitioning, not integer division. Candidate shapes should include two and three adjacent rows, aligned and staggered around the same screen axis, subject to actual row capacity and modules. Examples worth comparing for 15 include 8+7, 5+5+5, 6+5+4, and module-respecting 4+4+4+3—not because any is universally correct, but because each can be Pareto-optimal on a different map. For 20, compare 10+10, 7+7+6, 5x4, and module-respecting variants. Reject any pattern that strands one person or cuts substantially more high-value social edges for a trivial geometry gain.

### Families with children

Parent/caregiver adjacency and supervision are hard or near-hard constraints. Favor easy egress when bathroom trips are likely; community reports repeatedly identify this need ([family example](https://www.reddit.com/r/AITAH/comments/1le50v9/mom_demands_i_sit_somewhere_else_because_she/)). Sightline uncertainty is higher for children; the EDCF guide recommends booster cushions where needed. Do not infer a child’s safe independence from party size.

### Coworkers or mixed-affinity groups

Do not infer romantic/intimate module preferences. If relationship details are unknown, optimize compactness and fairness with weak, roughly equal social edges; keep the group in adjacent rows rather than a very long line when that reduces edge-seat harm. If subteams/plus-ones are known, preserve those ties without creating a visibly isolated outsider.

### 3D and immersive formats

Apply a stronger penalty to lateral off-axis viewing and extreme field of view. Research on stereoscopic displays associates viewing distance/direction with fatigue and discomfort, but effects are nuanced and study-specific ([visual-fatigue study](https://pmc.ncbi.nlm.nih.gov/articles/PMC5510554/), [203-participant cinema study](https://pubmed.ncbi.nlm.nih.gov/22733100/)). For IMAX, 4DX, ScreenX, and D-BOX, use venue/format profiles and module topology; do not transfer a conventional-auditorium row fraction blindly.

## Learning and validation plan

The research does not justify freezing universal weights. ScreenWatch should learn carefully:

- Log the entire available seat map at decision time, not only the chosen seats; otherwise proximity and availability effects are confounded.
- Ask for lightweight outcome feedback: “Great / acceptable / bad,” plus optional reasons (too close, too far, off-center, split, strangers, access, sound, module).
- Separate revealed preference from constrained choice; a user selecting the least-bad remaining seat is not evidence that they love it.
- Maintain venue/auditorium profiles because row labels and geometry recur.
- Use hierarchical priors so new users get evidence-based defaults while repeat users personalize quickly.
- Evaluate top-k acceptance, post-show satisfaction, worst-member dissatisfaction, split regret, accessibility violations (target zero), recommendation stability, and calibration—not merely whether the user clicked the first suggestion.
- Run counterfactual replay on saved seat maps and adversarial fixtures for irregular rows, gaps, ambiguous modules, 3/5-person pods, 15/20-person parties, sold-out rooms, and accessible groups.

## What remains unknown

- Public seat maps rarely expose exact screen dimensions, elevation, screen gain, obstructions, or calibrated acoustic coverage. Geometry estimates must carry uncertainty.
- There is little direct peer-reviewed evidence comparing social layouts for cinema groups of exact sizes 3, 5, 15, or 20. Those patterns must be derived from general social/allocation principles and validated in product.
- “Date,” “friends,” and “coworkers” are weak labels for real relationship graphs. A single optional “who should sit together?” interaction is more informative and less stereotyped.
- Reddit is valuable for failure-mode discovery but is self-selected, culturally uneven, and cannot set universal weights.
- Venue orphan-seat and sofa-purchase rules can vary by chain, auditorium, sales channel, occupancy, and time; parsers and checkout validation remain authoritative.

## Research-backed product position

ScreenWatch should not claim to know a single objectively perfect seat. It can credibly claim to compute the best available **party arrangement** from the real map, explain the tradeoffs, learn the user’s preferences, protect accessibility and social ties, and offer alternatives when “best view” conflicts with “stay together.” That is both more honest and more technically ambitious than a collection of hard-coded group-size patterns.
