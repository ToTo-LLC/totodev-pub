

-- Usage digest represents a summary of verbatim items
-- from a given lease_number related to usage and exclusives
CREATE TABLE usage_digest (
      master_property VARCHAR NOT NULL
    , lease_number VARCHAR NOT NULL

    , use_summary VARCHAR
    , exclusive_summary VARCHAR
    , biz_name VARCHAR NOT NULL

    -- Below are "checksum" fields used to verify that the cache is up-to-date 
    , verbatim_newest_execution DATE NOT NULL
    , verbatim_item_count INT NOT NULL
    , verbatim_char_count INT NOT NULL
    ,PRIMARY KEY (master_property, lease_number)
);



