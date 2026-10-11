{#
  Exact schema names. DuckDB: raw, staging, core, semantic inside the
  "peoplelens" catalog. BigQuery: datasets peoplelens_raw, peoplelens_staging,
  peoplelens_core, peoplelens_semantic.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {%- if custom_schema_name is none -%}
    {{ target.schema }}
  {%- elif target.type == 'bigquery' -%}
    peoplelens_{{ custom_schema_name | trim }}
  {%- else -%}
    {{ custom_schema_name | trim }}
  {%- endif -%}
{%- endmacro %}
