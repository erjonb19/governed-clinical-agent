"""A small synthetic FHIR Gold with the real table and column names.

The real FHIR Gold is built from Synthea and is not in the repo, so CI has
none. The PHI tests need one: small-cell suppression needs real groups, and the
output check needs real values. This builds one in a tmp dir:

  gold_patient: 15 patients -- 12 female, 3 male (so "male" is a small cell)
  gold_condition: 14 Hypertension patients, 4 Rare-disease patients
  marital_status holds a date-shaped string for one group, and state a
  zip-shaped one, so the output check's value rules have something to catch
  in columns the pre-check rightly allows.
"""

import duckdb

PATIENTS = 15


def build(path: str) -> str:
    con = duckdb.connect(path)
    con.execute("""
        CREATE TABLE gold_patient AS
        SELECT 'p' || i AS patient_id, 'Given' || i AS given_name, 'Family' || i AS family_name,
               CASE WHEN i < 12 THEN 'female' ELSE 'male' END AS gender,
               '19' || (50 + i) || '-0' || (1 + i % 9) || '-1' || (i % 9) AS birth_date,
               false AS is_deceased,
               CASE WHEN i < 12 THEN '2019-03-05' ELSE 'S' END AS marital_status,
               'Springfield' AS city,
               CASE WHEN i < 12 THEN '02134' ELSE 'MA' END AS state,
               '0213' || (i % 10) AS postal_code,
               40 + i AS age_years
        FROM range(__PATIENTS__) t(i)
    """.replace("__PATIENTS__", str(PATIENTS)))
    con.execute("""
        CREATE TABLE gold_condition AS
        SELECT 'c' || i AS condition_id, 'p' || i AS patient_id, 'e' || i AS encounter_id,
               'SNOMED' AS code_system, '38341003' AS code,
               CASE WHEN i < 14 THEN 'Hypertension' ELSE 'Other' END AS condition_name,
               'active' AS clinical_status, '2020-01-0' || (1 + i % 9) AS onset_date,
               NULL AS abatement_date, true AS is_active, 'female' AS gender, 'MA' AS state,
               50 AS age_at_onset
        FROM range(__PATIENTS__) t(i)
        UNION ALL
        SELECT 'r' || i, 'p' || i, 'e' || i, 'SNOMED', '1', 'Rare disease', 'active',
               '2021-02-02', NULL, true, 'female', 'MA', 30
        FROM range(4) t(i)
    """.replace("__PATIENTS__", str(PATIENTS)))
    con.close()
    return path
