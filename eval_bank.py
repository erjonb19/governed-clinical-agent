"""
eval_bank.py
============
The evaluation question bank. Each case is a natural-language question paired
with trusted reference SQL and how to score it. Tiered by difficulty so the
report shows where the agent is strong and where it breaks down.

Grow this file: copy a case, change the question and reference_sql, keep the
mode/key_columns pattern. eval_harness.py picks up new cases automatically.

Schema (gold_hospital_profile): facility_id, facility_name, city, state, zip,
star_rating, mspb_score, readmit_hwr, readmit_hf, readmit_pn, readmit_ami,
readmit_copd, ed_median_min, ed_psych_median_min, ed_left_before_seen_pct,
ed_volume.

Comparison modes:
  scalar : result is one value (count, avg, min, max). Compared numerically.
  keyed  : result is an ordered list. Compared on key_columns, in order.

Tiers:
  1 -- simple aggregates (one measure, no filter)
  2 -- filtered aggregates (a WHERE condition)
  3 -- rankings / ordered lists (ORDER BY + LIMIT, tie-break matters)
  4 -- multi-condition reasoning (several filters or a computed ranking)

Tie-break convention: every ordered query breaks ties on facility_id ASC so the
expected answer is deterministic. The agent must do the same to match exactly.
"""

SUBSET_IDS = {
    "t1_count_all",
    "t1_max_psych_ed",
    "t2_avg_mspb_pa",
    "t3_highest_mspb_top5",
    "t4_best_value_top10",
    "t4_lowest_ratio_top5",
}

CASES = [
    # ===================== tier 1: simple aggregates =====================
    {
        "id": "t1_count_all", "tier": 1,
        "question": "How many hospitals are in the dataset in total?",
        "reference_sql": "SELECT count(*) AS n FROM gold_hospital_profile",
        "mode": "scalar",
    },
    {
        "id": "t1_count_5star", "tier": 1,
        "question": "How many hospitals have an overall star rating of exactly 5?",
        "reference_sql": "SELECT count(*) AS n FROM gold_hospital_profile WHERE star_rating = 5",
        "mode": "scalar",
    },
    {
        "id": "t1_count_rated", "tier": 1,
        "question": "How many hospitals have a non-null overall star rating?",
        "reference_sql": "SELECT count(*) AS n FROM gold_hospital_profile WHERE star_rating IS NOT NULL",
        "mode": "scalar",
    },
    {
        "id": "t1_avg_mspb", "tier": 1,
        "question": "What is the average Medicare spending per beneficiary score across all hospitals that have one? Round to 4 decimals.",
        "reference_sql": "SELECT round(avg(mspb_score), 4) AS v FROM gold_hospital_profile WHERE mspb_score IS NOT NULL",
        "mode": "scalar",
    },
    {
        "id": "t1_max_psych_ed", "tier": 1,
        "question": "What is the highest psychiatric ED median wait time in the data?",
        "reference_sql": "SELECT max(ed_psych_median_min) AS v FROM gold_hospital_profile",
        "mode": "scalar",
    },
    {
        "id": "t1_min_hwr", "tier": 1,
        "question": "What is the lowest hospital-wide readmission rate in the data?",
        "reference_sql": "SELECT min(readmit_hwr) AS v FROM gold_hospital_profile",
        "mode": "scalar",
    },
    {
        "id": "t1_avg_star", "tier": 1,
        "question": "What is the average overall star rating across all rated hospitals? Round to 2 decimals.",
        "reference_sql": "SELECT round(avg(star_rating), 2) AS v FROM gold_hospital_profile WHERE star_rating IS NOT NULL",
        "mode": "scalar",
    },
    {
        "id": "t1_distinct_states", "tier": 1,
        "question": "How many distinct states are represented in the dataset?",
        "reference_sql": "SELECT count(DISTINCT state) AS n FROM gold_hospital_profile",
        "mode": "scalar",
    },

    # ===================== tier 2: filtered aggregates =====================
    {
        "id": "t2_count_hf_data", "tier": 2,
        "question": "How many hospitals have a non-null heart failure readmission rate?",
        "reference_sql": "SELECT count(*) AS n FROM gold_hospital_profile WHERE readmit_hf IS NOT NULL",
        "mode": "scalar",
    },
    {
        "id": "t2_avg_mspb_pa", "tier": 2,
        "question": "What is the average MSPB score for hospitals in Pennsylvania (PA)? Round to 4 decimals.",
        "reference_sql": "SELECT round(avg(mspb_score), 4) AS v FROM gold_hospital_profile WHERE state = 'PA' AND mspb_score IS NOT NULL",
        "mode": "scalar",
    },
    {
        "id": "t2_count_ny_5star", "tier": 2,
        "question": "How many hospitals in New York (NY) have a 5-star rating?",
        "reference_sql": "SELECT count(*) AS n FROM gold_hospital_profile WHERE state = 'NY' AND star_rating = 5",
        "mode": "scalar",
    },
    {
        "id": "t2_avg_hwr_ma", "tier": 2,
        "question": "What is the average hospital-wide readmission rate for Massachusetts (MA) hospitals? Round to 2 decimals.",
        "reference_sql": "SELECT round(avg(readmit_hwr), 2) AS v FROM gold_hospital_profile WHERE state = 'MA' AND readmit_hwr IS NOT NULL",
        "mode": "scalar",
    },
    {
        "id": "t2_count_nj", "tier": 2,
        "question": "How many hospitals are located in New Jersey (NJ)?",
        "reference_sql": "SELECT count(*) AS n FROM gold_hospital_profile WHERE state = 'NJ'",
        "mode": "scalar",
    },
    {
        "id": "t2_count_cheap", "tier": 2,
        "question": "How many hospitals have an MSPB score below 0.90?",
        "reference_sql": "SELECT count(*) AS n FROM gold_hospital_profile WHERE mspb_score < 0.90",
        "mode": "scalar",
    },
    {
        "id": "t2_avg_psych_ny", "tier": 2,
        "question": "What is the average psychiatric ED median wait time for New York (NY) hospitals that report it? Round to 1 decimal.",
        "reference_sql": "SELECT round(avg(ed_psych_median_min), 1) AS v FROM gold_hospital_profile WHERE state = 'NY' AND ed_psych_median_min IS NOT NULL",
        "mode": "scalar",
    },
    {
        "id": "t2_count_highstar", "tier": 2,
        "question": "How many hospitals have a star rating of 4 or higher?",
        "reference_sql": "SELECT count(*) AS n FROM gold_hospital_profile WHERE star_rating >= 4",
        "mode": "scalar",
    },

    # ===================== tier 3: rankings / ordered lists =====================
    {
        "id": "t3_low_hf_top10", "tier": 3,
        "question": "List the facility_id of the 10 hospitals with the lowest heart failure readmission rate, lowest first. Include facility_id.",
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile WHERE readmit_hf IS NOT NULL "
                          "ORDER BY readmit_hf ASC, facility_id ASC LIMIT 10"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t3_worst_psych_ed_1", "tier": 3,
        "question": "Which single hospital has the highest psychiatric ED median wait time? Give its facility_id.",
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile WHERE ed_psych_median_min IS NOT NULL "
                          "ORDER BY ed_psych_median_min DESC, facility_id ASC LIMIT 1"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t3_highest_mspb_top5", "tier": 3,
        "question": "List the facility_id of the 5 most expensive hospitals by MSPB score, highest first.",
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile WHERE mspb_score IS NOT NULL "
                          "ORDER BY mspb_score DESC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t3_fastest_ed_top10", "tier": 3,
        "question": "List the facility_id of the 10 hospitals with the shortest overall ED median wait time, shortest first.",
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile WHERE ed_median_min IS NOT NULL "
                          "ORDER BY ed_median_min ASC, facility_id ASC LIMIT 10"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t3_low_pn_top5", "tier": 3,
        "question": "List the facility_id of the 5 hospitals with the lowest pneumonia readmission rate, lowest first.",
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile WHERE readmit_pn IS NOT NULL "
                          "ORDER BY readmit_pn ASC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t3_worst_lbs_top5", "tier": 3,
        "question": "List the facility_id of the 5 hospitals with the highest percent of ED patients who left before being seen, highest first.",
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile WHERE ed_left_before_seen_pct IS NOT NULL "
                          "ORDER BY ed_left_before_seen_pct DESC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t3_cheapest_top10", "tier": 3,
        "question": "List the facility_id of the 10 cheapest hospitals by MSPB score, cheapest first.",
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile WHERE mspb_score IS NOT NULL "
                          "ORDER BY mspb_score ASC, facility_id ASC LIMIT 10"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },

    # ===================== tier 4: multi-condition reasoning =====================
    {
        "id": "t4_best_value_top10", "tier": 4,
        "question": ("List the facility_id of the 10 best-value hospitals, defined as 4 or 5 stars AND an MSPB score "
                     "below 1.0, ordered by MSPB ascending (cheapest first)."),
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile "
                          "WHERE star_rating >= 4 AND mspb_score < 1.0 "
                          "ORDER BY mspb_score ASC, facility_id ASC LIMIT 10"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t4_ny_lowcost_highqual", "tier": 4,
        "question": ("Among New York (NY) hospitals with a star rating of at least 4, list the facility_id of the 5 "
                     "with the lowest hospital-wide readmission rate, lowest first."),
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile "
                          "WHERE state = 'NY' AND star_rating >= 4 AND readmit_hwr IS NOT NULL "
                          "ORDER BY readmit_hwr ASC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t4_count_lowcost_highqual", "tier": 4,
        "question": "How many hospitals are both 5-star AND have an MSPB score below 0.95?",
        "reference_sql": "SELECT count(*) AS n FROM gold_hospital_profile WHERE star_rating = 5 AND mspb_score < 0.95",
        "mode": "scalar",
    },
    {
        "id": "t4_pa_fast_ed_good", "tier": 4,
        "question": ("Among Pennsylvania (PA) hospitals with an overall ED median wait under 180 minutes, list the "
                     "facility_id of the 5 with the highest star rating, highest first."),
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile "
                          "WHERE state = 'PA' AND ed_median_min < 180 AND star_rating IS NOT NULL "
                          "ORDER BY star_rating DESC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t4_lowest_ratio_top5", "tier": 4,
        "question": ("Define value as star_rating divided by mspb_score. Among hospitals that have both a star rating "
                     "and an MSPB score, list the facility_id of the 5 with the highest value ratio, highest first."),
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile "
                          "WHERE star_rating IS NOT NULL AND mspb_score IS NOT NULL AND mspb_score > 0 "
                          "ORDER BY (star_rating / mspb_score) DESC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t4_multistate_5star_lowhf", "tier": 4,
        "question": ("Among hospitals in NY, NJ, or CT that are 5-star, list the facility_id of the 5 with the lowest "
                     "heart failure readmission rate, lowest first."),
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile "
                          "WHERE state IN ('NY','NJ','CT') AND star_rating = 5 AND readmit_hf IS NOT NULL "
                          "ORDER BY readmit_hf ASC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t4_count_fast_lowlbs", "tier": 4,
        "question": "How many hospitals have an overall ED median wait under 150 minutes AND a left-before-seen percent under 2?",
        "reference_sql": ("SELECT count(*) AS n FROM gold_hospital_profile "
                          "WHERE ed_median_min < 150 AND ed_left_before_seen_pct < 2"),
        "mode": "scalar",
    },

    # ===================== tier 5: hard / failure-prone =====================
    # These exist to give SELF-CORRECTION something to actually fix. The
    # single-shot path has no headroom at 30/30, so measuring whether retry
    # helps requires cases where a first attempt plausibly fails. These are hard
    # for REAL reasons -- ambiguous phrasing, clinical vocabulary that does not
    # map 1:1 to a column, unusual aggregations -- not artificially rigged.
    {
        "id": "t5_clinical_vocab_copd", "tier": 5,
        "question": ("Among facilities reporting it, what is the average 30-day all-cause readmission rate "
                     "for patients with chronic obstructive pulmonary disease? Round to 2 decimals."),
        "reference_sql": ("SELECT round(avg(readmit_copd), 2) AS v FROM gold_hospital_profile "
                          "WHERE readmit_copd IS NOT NULL"),
        "mode": "scalar",
    },
    {
        "id": "t5_boarding_gap", "tier": 5,
        "question": ("Behavioral health boarding gap: among facilities that report BOTH an overall ED median time "
                     "and a psychiatric ED median time, list the facility_id of the 5 with the largest difference "
                     "(psychiatric minus overall), largest gap first."),
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile "
                          "WHERE ed_psych_median_min IS NOT NULL AND ed_median_min IS NOT NULL "
                          "ORDER BY (ed_psych_median_min - ed_median_min) DESC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t5_cost_efficiency_ratio", "tier": 5,
        "question": ("Cost efficiency: among hospitals with both measures, list the facility_id of the 5 with the "
                     "lowest ratio of MSPB score to star rating, lowest ratio first."),
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile "
                          "WHERE mspb_score IS NOT NULL AND star_rating IS NOT NULL AND star_rating > 0 "
                          "ORDER BY (mspb_score / star_rating) ASC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    {
        "id": "t5_above_avg_count", "tier": 5,
        "question": ("How many hospitals have a hospital-wide readmission rate ABOVE the overall average "
                     "hospital-wide readmission rate across all facilities that report one?"),
        "reference_sql": ("SELECT count(*) AS n FROM gold_hospital_profile "
                          "WHERE readmit_hwr > (SELECT avg(readmit_hwr) FROM gold_hospital_profile "
                          "WHERE readmit_hwr IS NOT NULL)"),
        "mode": "scalar",
    },
    {
        "id": "t5_condition_spread", "tier": 5,
        "question": ("Among facilities reporting both, list the facility_id of the 5 hospitals where the heart "
                     "failure readmission rate exceeds the pneumonia readmission rate by the greatest margin, "
                     "largest margin first."),
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile "
                          "WHERE readmit_hf IS NOT NULL AND readmit_pn IS NOT NULL "
                          "ORDER BY (readmit_hf - readmit_pn) DESC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
    # --- ed_volume: a CATEGORY, not a number ------------------------------
    # This column was DOUBLE and entirely NULL until the build was corrected to
    # keep CMS's text bucket. These cases exist so it cannot quietly go back to
    # being empty: with no data behind it, every one of them returns nothing and
    # fails, instead of passing by comparing NULL against NULL the way a scalar
    # case over an empty column does.
    {
        "id": "t2_count_very_high_ed", "tier": 2,
        "question": "How many hospitals have a very high emergency department volume?",
        "reference_sql": ("SELECT count(*) AS n FROM gold_hospital_profile "
                          "WHERE ed_volume = 'very high'"),
        "mode": "scalar",
    },
    {
        "id": "t3_ed_volume_breakdown", "tier": 3,
        # The column is named explicitly, as it is in every other keyed case
        # ("list the facility_id of..."). Asking for "the category names" let a
        # correct answer come back aliased as `category`, which the keyed
        # comparison scored as a miss -- an artefact of the question, not a
        # fault in the SQL.
        "question": ("Break down the hospitals by emergency department volume, returning the "
                     "ed_volume column, with the most common volume category first."),
        "reference_sql": ("SELECT ed_volume FROM gold_hospital_profile "
                          "WHERE ed_volume IS NOT NULL GROUP BY ed_volume "
                          "ORDER BY count(*) DESC, ed_volume ASC"),
        "mode": "keyed", "key_columns": ["ed_volume"],
    },
    {
        "id": "t4_high_volume_worst_wait", "tier": 4,
        "question": ("Among hospitals whose emergency department volume is high or very high, list the "
                     "facility_id of the 5 with the longest median ED wait, longest first."),
        "reference_sql": ("SELECT facility_id FROM gold_hospital_profile "
                          "WHERE ed_volume IN ('high','very high') AND ed_median_min IS NOT NULL "
                          "ORDER BY ed_median_min DESC, facility_id ASC LIMIT 5"),
        "mode": "keyed", "key_columns": ["facility_id"],
    },
]
