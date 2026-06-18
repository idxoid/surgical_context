# Структурные роли и правила (Pass 1)

Источник истины — [context_engine/indexer/role_cascade.py](../context_engine/indexer/role_cascade.py).
Документ — снимок состояния `role_cascade.py` на коммит `640f157`. Если код
разойдётся с таблицами, **код прав**, документ обновляется отдельно.

## 1. Пайплайн

```
FanProfile (per-symbol fan signals)
         │
         ▼
   assign_l1()            ← L1 router (cascade-of-if, первый совпавший побеждает)
         │
         ▼
   L1 bucket ∈ { noise, routing_wrap, control_flow, state_types,
                 boundary_integration, compute_leaf, unclassified }
         │
         ▼
   _matching_roles()      ← перебирает L2_PREDICATES по убыванию specificity
         │                  фильтрует: pred.l1 ∈ {None, l1}
         │                  дедуп по role-имени (берёт первое совпадение)
         ▼
   list[(role, specificity)]
         │
         ▼
   assign_symbol_roles()
   • primary  = hits[0].role
   • supp[0:3] = hits[1..MAX_SUPPORTING].role
   • если hits=[] → primary = L1_FALLBACK_ROLE[l1]
```

**Константы:**
- `_EPS = 0.05` — порог "нулевого" сигнала
- `_RUNTIME_CALL_IN_MIN = 1.0`
- `_INTEGRATION_CALL_RATIO_MIN = 0.35`
- `_INTEGRATION_IMPORT_MIN = 2.0`
- `MAX_SUPPORTING = 3` — максимум supporting ролей на символ
- `DEFAULT_MIN_SUPPORT = 2`, `RARE_ROLE_MIN_SUPPORT = 1`

## 2. L1 router — порядок ветвей

Каждый шаг — первый-совпавший-побеждает; ниже идёт только если предыдущее условие False.

| # | bucket | условие |
|---|---|---|
| 1 | **noise** | `zero_in_degree AND call_fan_out ≤ _EPS AND not surface_owner AND not is_proxy_binding AND not is_polymorphic_factory` |
| 2 | **routing_wrap** | `is_proxy_binding OR handle_fan_in > _EPS OR handle_fan_out > _EPS OR decorated_in > _EPS` |
| 3 | **state_types** | `inherits_builtin_exception OR type_fan_in > max(_EPS, call_fan_out) OR depend_fan_in > max(_EPS, call_fan_out)` |
| 3b | **state_types** (расширенный) | `(is_class AND (type_fan_in OR depend_fan_in OR api_fan_in OR api_fan_out > _EPS)) OR api_fan_out > _EPS` |
| 4 | **boundary_integration** | `_integration_boundary_signal(row)` — см. вспомогательное определение ниже |
| 5 | **control_flow** | `(call_fan_out > call_fan_in AND call_fan_out > _EPS) OR is_polymorphic_factory` |
| 6 | **compute_leaf** | `call_fan_in > _EPS AND call_leaf` |
| 6b | **control_flow / compute_leaf** | если есть `call_fan_in OR call_fan_out` — то по сравнению call_out vs call_in |
| 7 | **unclassified** | если ничего не совпало |

**Вспомогательные:**

```python
surface_owner = (
    api_fan_out > _EPS
    OR (is_class AND has_documentation)
    OR reexport_in > _EPS              # ← публичный ре-экспорт = публичная поверхность
)
is_polymorphic_factory = (construct_fan_out >= 2 AND type_fan_out_return > _EPS)

_integration_boundary_signal(row) =
    external_integration_call_fan_out > _EPS
    AND type_fan_in <= max(_EPS, call_fan_out * 2.0)   # не type-hub
    AND call_fan_out > call_fan_in                       # больше зовёт чем зовут её
    AND external_integration_out_ratio >= 0.35           # ≥35% исходящих — во внешние integration пакеты
```

## 3. L2 предикаты — по L1 buckets

Перебираются по убыванию `specificity`. Дедуп по role-имени.

### routing_wrap

| spec | role | условие |
|---|---|---|
| 90 | proxy_mechanism | `is_proxy_binding` |
| 86 | factory_surface | `returns_function_expression AND handle_fan_out > _EPS` (higher-order factory) |
| 85 | interceptor | `decorated_in > _EPS AND handle_fan_out ≤ _EPS AND type_fan_in_param < max(1, call_fan_in)` |
| 85 | registration_step | `handle_fan_out > _EPS` |
| 78 | composition_surface | `is_class AND decorator_arg_ref_count >= 3` (NestJS `@Module({...})` форма) |
| 70 | executor | `handle_fan_in > _EPS` |

### control_flow

| spec | role | условие |
|---|---|---|
| 78 | request_router | `handle_fan_out ≤ _EPS AND handle_fan_in ≤ _EPS AND call_fan_in ≥ 1.0 AND handler_call_fan_out > _EPS` |
| 77 | api_surface | `depth_from_public ≤ 1 AND call_fan_out > call_fan_in AND ((has_documentation AND (api_fan_in OR doc_definition_weight)) OR import_in ≥ 10)` |
| 75 | binding_surface (Pattern A) | `is_function AND assembles_mapping_in_loop AND attr_reads_fan_out >= 1.0 AND call_fan_out > _EPS` — for-loop + subscript-write + читает атрибуты |
| 75 | binding_surface (Pattern B) | `is_function AND (returns_mapping OR returns_constructed_type) AND attr_reads_fan_out >= 3.0 AND call_fan_out > _EPS` — собирает значения и пакует |
| 74 | registration_step | `depth_from_public ≤ 1 AND call_fan_out > call_fan_in AND construct_fan_out > _EPS AND import_in ≥ 10` |
| 73 | binding_surface (legacy topology) | `call_fan_out > call_fan_in AND type_fan_out > _EPS AND cross_package_call_out ≥ 1.0 AND import_in ≥ 20 AND depth_from_public ≥ 2` |
| 72 | dependency_solver | `type_fan_in_isinstance > _EPS OR inject_fan_in > _EPS OR (type_fan_out > _EPS AND cross_package_call_out >= 2.0 AND import_in >= 20 AND depth_from_public >= 2)` |
| 71 | schema_builder | `call_fan_out > _EPS AND ((type_fan_in > _EPS AND type_fan_in_return ≤ _EPS) OR (construct_fan_out > _EPS AND depth_from_public ≤ 1) OR (type_fan_out > _EPS AND call_fan_out > call_fan_in AND import_in >= 20))` |
| 71 | runtime_surface | `call_fan_in > _EPS AND call_fan_out > _EPS AND depth_from_public ≤ 2 AND import_in >= 10` |
| 70 | orchestrator | `call_fan_out > call_fan_in AND call_fan_out > _EPS` |
| 66 | composition_surface | `call_fan_out > call_fan_in AND cross_package_call_out ≥ 1.0 AND import_in ≥ 2` |
| 65 | factory_surface | `((construct_fan_out OR type_fan_out_return) AND call_fan_out > _EPS) OR (construct_fan_out >= 2 AND type_fan_out_return > _EPS)` |

### state_types

| spec | role | условие |
|---|---|---|
| 88 | error_surface | `inherits_builtin_exception` (AST-маркер на `class X(...Exception)`) |
| 85 | abstract_contract | `is_class AND depend_fan_in > max(_EPS, type_fan_in_param * 1.5) AND call_fan_in ≤ _EPS` |
| 80 | config_surface | `is_class AND NOT (reexport_in AND api_fan_out AND call_fan_in) AND (type_fan_in_param > max(_EPS, call_fan_in) OR (type_fan_in_param ≤ _EPS AND depend_fan_in > _EPS AND type_fan_in_isinstance > _EPS AND type_fan_in ≤ max(1, call_fan_in)))` |
| 76 | composition_surface | `(is_class AND (decorator_arg_ref_count >= 3 OR fluent_self_return_count >= 2)) OR (api_fan_out >= 3 AND api_fan_out > type_fan_in)` |
| 75 | representation_surface | `is_class AND type_fan_in > max(_EPS, call_fan_out * 2.0)` |
| 70 | api_surface | `(depth_from_public ≤ 1 AND has_documentation AND (api_fan_in OR doc_definition_weight)) OR (depth_from_public ≤ 1 AND api_fan_out > _EPS) OR (is_function AND depth_from_public ≤ 1 AND depend_fan_in > _EPS AND call_fan_out > _EPS) OR (is_class AND reexport_in > _EPS AND api_fan_out > _EPS)` |
| 69 | runtime_surface | `is_class AND call_fan_in > _EPS AND (type_fan_in_param > _EPS OR reexport_in > _EPS)` |
| 68 | registration_step | `is_class AND reexport_in > _EPS AND api_fan_out > _EPS AND call_fan_in > _EPS` |
| 67 | dependency_solver | `(is_function AND depend_fan_in > _EPS AND depth_from_public ≤ 1) OR (is_class AND type_fan_in_isinstance > _EPS)` |

### boundary_integration

| spec | role | условие |
|---|---|---|
| 80 | integration_surface | `external_integration_call_fan_out > _EPS OR (external_integration_import_fan_out >= 2.0 AND external_integration_call_fan_out > _EPS)` |

### compute_leaf

| spec | role | условие |
|---|---|---|
| 85 | executor | `handle_fan_in > _EPS` |
| 80 | validator_handle | `type_fan_in > _EPS AND call_fan_in > _EPS AND handle_fan_in ≤ _EPS` |
| 78 | request_router | `handle_fan_out ≤ _EPS AND handle_fan_in ≤ _EPS AND call_fan_in ≥ 1.0 AND handler_call_fan_out > _EPS` |
| 75 | core_runtime | `call_fan_in > _EPS AND call_leaf AND handle_fan_in ≤ _EPS AND type_fan_in ≤ max(1, call_fan_in)` |
| 72 | runtime_surface | `call_fan_in > _EPS AND NOT call_leaf AND depth_from_public ≤ 2 AND import_in >= 8` |
| 60 | executor | `call_leaf AND call_fan_in > _EPS AND is_function` |

### cross-bucket (l1=None)

Срабатывает в **любом** L1, потому что `pred.l1 is None` пропускает фильтр в `_matching_roles`.

| spec | role | условие | пояснение |
|---|---|---|---|
| 68 | integration_surface | `external_integration_call_fan_out > 1.5` | для thick API surfaces (Celery `Task.apply_async`) которые не проходят boundary_integration gate из-за низкого ratio — приходит в supp поверх их primary api_surface |

## 4. Fallback roles

Когда ни один L2 предикат в L1-бакете не сработал, primary = fallback:

| L1 bucket | fallback primary |
|---|---|
| routing_wrap | runtime_surface |
| control_flow | orchestrator |
| state_types | representation_surface |
| boundary_integration | integration_surface |
| compute_leaf | core_runtime |
| noise | **orphan** |
| unclassified | supporting_surface |

## 5. Rare roles (более низкий порог presence-gate)

Появляются в `RARE_ROLES`, для них достаточно `RARE_ROLE_MIN_SUPPORT = 1` экземпляра в workspace; остальные требуют `DEFAULT_MIN_SUPPORT = 2`.

```
proxy_mechanism, interceptor, abstract_contract, registration_step,
request_router, dependency_solver, schema_builder, integration_surface
```

## 6. Глоссарий fan-сигналов (FanProfile)

| signal | смысл |
|---|---|
| `call_fan_in/out` | входящие/исходящие CALLS_* edges |
| `type_fan_in/out` | USES_TYPE — символ используется/использует как тип |
| `type_fan_in_param/isinstance/return` | подвиды USES_TYPE по позиции (param annotation / isinstance check / return type) |
| `api_fan_in/out` | HAS_API + INHERITED_API |
| `handle_fan_in/out` | HANDLES — диспетчер/обработчик |
| `handler_call_fan_out` | call_out специфически в handler-target'ы |
| `decorated_in/out` | DECORATED_BY |
| `construct_fan_out` | INSTANTIATES |
| `inject_fan_in` | INJECTS — DI-target |
| `depend_fan_in/out` | DEPENDS_ON |
| `decorator_arg_ref_count` | COMPOSES — кол-во распакованных ссылок в декораторе вида `@Module({...})` |
| `fluent_self_return_count` | методы класса с return-type = сам класс (`QuerySet.filter()→QuerySet`) |
| `attr_reads_fan_out` / `attr_writes_*` | READS_ATTR / WRITES_ATTR (AST-marker, не call) |
| `cross_package_call_in/out` | call edges пересекающие package boundary |
| `depth_from_public` | дистанция от ближайшей публичной поверхности |
| `import_in` | сколько разных файлов импортирует этот символ |
| `reexport_in` | сколько публичных `__init__` / index files ре-экспортируют символ |
| `doc_anchor_count`, `doc_definition_weight` | doc-сигналы |
| `is_proxy_binding` | AST-marker (метаклассный/proxy паттерн) |
| `external_call_fan_out`, `external_import_fan_out`, `external_root_count` | внешние пакеты (включая stdlib) |
| `external_integration_*` | то же, но **фильтрованное** через `EXTERNAL_INTEGRATION_PLUMBING_ROOTS` (стдлиб/тестовый плумбинг исключён) |
| `inherits_builtin_exception` | AST-marker: класс наследует встроенный Exception |
| `returns_function_expression` | AST-marker: тело возвращает функ-выражение / стрелку |
| `returns_mapping`, `returns_sequence`, `returns_constructed_type` | AST-marker: shape возвращаемого значения |
| `iterates_attr_call`, `assembles_mapping_in_loop` | AST-marker: цикл-итерация с собором мапы |

## 7. Принципы (engineering invariants)

Из [docs/engineering_principles.md](engineering_principles.md):

- **Структурно-только.** Никаких имён / file-stem / query→role / symbol-name→role таблиц.
- **Граф = derivative кода и топологии.** Бенчмарк измеряет, не авторизует роли.
- **Чинить движок, а не симптом.** Если предикат недосчитывает — добавить структурный edge / AST-сигнал / type-hop, а не reactive threshold.
- **Precision > recall** для derived edges. Если сигнал требует dataflow — честно зафиксировать как gap, а не подменить эвристикой.
- **Валидировать эмпирически.** Каждое изменение предиката измеряется на индексированном бенчмарке; регрессии репортятся.

Бывшие antipatterns (удалены, не возвращать):
`_target_query_bonus`, `_GENERIC_AUTO_ROLE_PLANS`, `infer_*_roles`,
`worker_execution`, любые фикстуры query→role.
