// Production entry (wrangler deploy --env production): bundles the BigQuery-dialect catalog.
import catalogJson from "./generated/catalog.bigquery.json";
import type { CatalogFile } from "./catalog/catalog";
import { createWorker } from "./index";

export { UserDO } from "./session/userDO";

export default createWorker(catalogJson as unknown as CatalogFile);
