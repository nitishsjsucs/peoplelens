{# Whole days from start to end (end minus start), via the cross-database dbt.datediff. #}
{% macro pl_days_between(start_expr, end_expr) -%}
  {{ dbt.datediff(start_expr, end_expr, 'day') }}
{%- endmacro %}
