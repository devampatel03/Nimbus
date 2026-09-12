-- Copyright (c) Nimbus. All rights reserved.
-- Private, unpublished, proprietary. Do not distribute.

CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;

LOAD 'age';
SET search_path = ag_catalog, "$user", public;
