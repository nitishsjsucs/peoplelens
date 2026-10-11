{#
  BigQuery-only model configs, returned as a dict so models can call
  config(**pl_bq_config(...)). On DuckDB they are no-ops (empty dict).

  authorized_view_of: dataset suffix (for example 'core') that this view must be
  authorized to read. dbt-bigquery's grant_access_to adds the view to that
  dataset's access list, which is what makes it an authorized view.
#}
{% macro pl_bq_config(authorized_view_of=none, layer=none) -%}
  {%- set cfg = {} -%}
  {%- if target.type == 'bigquery' -%}
    {%- if authorized_view_of is not none -%}
      {%- do cfg.update({'grant_access_to': [{'project': target.project, 'dataset': 'peoplelens_' ~ authorized_view_of}]}) -%}
    {%- endif -%}
    {%- if layer is not none -%}
      {%- do cfg.update({'labels': {'app': 'peoplelens', 'layer': layer}}) -%}
    {%- endif -%}
  {%- endif -%}
  {{ return(cfg) }}
{%- endmacro %}
