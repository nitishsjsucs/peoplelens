// Local entry (wrangler dev --local, vitest): bundles the DuckDB-dialect catalog.
import catalogJson from "./generated/catalog.duckdb.json";
import type { CatalogFile } from "./catalog/catalog";
import { createWorker } from "./index";

export { AccountBudgetDO } from "./session/accountBudgetDO";
export { UserDO } from "./session/userDO";

export default createWorker(catalogJson as unknown as CatalogFile);
