-- ─────────────────────────────────────────────────────────────────
-- sql/seed_lookups.sql
-- Seed data for dimension tables
--
-- Run once after schema.sql to populate reference tables.
-- All inserts are idempotent — safe to run multiple times.
--
--   psql -U postgres -d nigeria_health -f sql/seed_lookups.sql
-- ─────────────────────────────────────────────────────────────────


-- ── dim_diseases ─────────────────────────────────────────────────
-- Five NCDC-notifiable diseases tracked in this system.
-- ICD-10 codes included for clinical reference and interoperability.

INSERT INTO dim_diseases
    (disease_name, disease_code, category, transmission, is_notifiable)
VALUES
     ('Cholera',
      'A00',
      'Infectious',
      'Contaminated water and food',
      TRUE),

     ('Lassa Fever',
      'A96.2',
      'Viral Haemorrhagic Fever',
      'Rodent contact / Body fluids',
      TRUE),

     ('Mpox',
      'B04',
      'Zoonotic',
      'Animal/human contact / Droplets',
      TRUE),

     ('Meningitis',
      'G03',
      'Infectious / Bacterial',
      'Respiratory droplets / Close contact',
      TRUE),

     ('Yellow Fever',
      'A95',
      'Arboviral',
      'Mosquito-borne',
      TRUE)

ON CONFLICT (disease_name) DO UPDATE SET
    disease_code = EXCLUDED.disease_code,
    category     = EXCLUDED.category,
    transmission = EXCLUDED.transmission;


-- ── dim_states ───────────────────────────────────────────────────
-- All 36 Nigerian states plus the Federal Capital Territory.
-- Geometry and population are loaded by the ETL pipeline (load.py).
-- This seed populates names and zones so FK lookups work immediately.

INSERT INTO dim_states (state_name, geopolitical_zone)
VALUES
    -- North-East
    ('Adamawa',    'North-East'),
    ('Bauchi',     'North-East'),
    ('Borno',      'North-East'),
    ('Gombe',      'North-East'),
    ('Taraba',     'North-East'),
    ('Yobe',       'North-East'),
    -- North-West
    ('Jigawa',     'North-West'),
    ('Kaduna',     'North-West'),
    ('Kano',       'North-West'),
    ('Katsina',    'North-West'),
    ('Kebbi',      'North-West'),
    ('Sokoto',     'North-West'),
    ('Zamfara',    'North-West'),
    -- North-Central
    ('Benue',      'North-Central'),
    ('FCT',        'North-Central'),
    ('Kogi',       'North-Central'),
    ('Kwara',      'North-Central'),
    ('Nasarawa',   'North-Central'),
    ('Niger',      'North-Central'),
    ('Plateau',    'North-Central'),
    -- South-West
    ('Ekiti',      'South-West'),
    ('Lagos',      'South-West'),
    ('Ogun',       'South-West'),
    ('Ondo',       'South-West'),
    ('Osun',       'South-West'),
    ('Oyo',        'South-West'),
    -- South-South
    ('Akwa Ibom',  'South-South'),
    ('Bayelsa',    'South-South'),
    ('Cross River','South-South'),
    ('Delta',      'South-South'),
    ('Edo',        'South-South'),
    ('Rivers',     'South-South'),
    -- South-East
    ('Abia',       'South-East'),
    ('Anambra',    'South-East'),
    ('Ebonyi',     'South-East'),
    ('Enugu',      'South-East'),
    ('Imo',        'South-East')

ON CONFLICT (state_name) DO UPDATE SET
    geopolitical_zone = EXCLUDED.geopolitical_zone;


-- ── Verification queries ─────────────────────────────────────────
-- Uncomment and run manually to confirm seed data loaded correctly.

-- SELECT COUNT(*) AS disease_count FROM dim_diseases;   -- expected: 5
-- SELECT COUNT(*) AS state_count   FROM dim_states;     -- expected: 37

-- SELECT state_name, geopolitical_zone
-- FROM   dim_states
-- ORDER  BY geopolitical_zone, state_name;

-- SELECT disease_name, disease_code, transmission
-- FROM   dim_diseases
-- ORDER  BY disease_name;
