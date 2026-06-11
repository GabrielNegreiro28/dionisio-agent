from dataclasses import dataclass, field


@dataclass
class ToolDefinition:
    name: str
    domain: str
    destructive: bool
    description: str
    parameters_schema: dict
    method: str
    path_template: str
    path_params: list[str] = field(default_factory=list)
    # Compact description of the response shape (fields the planner may reference
    # via $stepN.path). Documents the ACTUAL return — so the planner never guesses
    # field names. See DATA_MODEL for shared entity shapes.
    returns: str = ""
    # If True, requires operator confirmation even though it is NOT destructive
    # (e.g. creating a campaign that fans out to many customers). Decouples
    # "should confirm" from "irreversible".
    requires_confirmation: bool = False

    def to_openai_tool(self) -> dict:
        """Returns OpenAI-compatible tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


# ---------------------------------------------------------------------------
# DATA MODEL — real entity shapes (derived from live API responses).
# The planner must reference ONLY these fields; never invent field names.
# ---------------------------------------------------------------------------

DATA_MODEL = """ENTIDADES (forma real, conforme o contrato OpenAPI da API):

Client:
  { id, name, phone, email?, cpf?, gender?:("masc"|"fem"),
    birthDate?:{day,month,year}, notes?,
    clientGroupIds?:[ "grp_vip" | "grp_corporativo" | ... ], createdAt:ms, updatedAt:ms }

ClientInsights:  (retorno de clients_insights)
  { clientId, visitsCount, noShowCount, totalSpent, spentThisMonth,
    couponsUsed, firstVisitAt:ms?, lastVisitAt:ms?, averageTicket }

Reservation:   (NÃO contém nome do cliente — apenas clientId)
  { id, clientId, areaId:("area_salao"|"area_varanda"|"area_mezanino"),
    tableId, duration:{ start:ms, end:ms }, partySize:{ adults, children, total },
    status:ReservationStatus, description?, cancellation?:{reasonCode,reasonText},
    createdAt:ms, updatedAt:ms }
  ReservationStatus ∈ ( pending | awaiting_payment | confirmed | seated | completed |
                        cancelled | cancelled_by_client | cancelled_by_business | no_show )
  (atenção: é "cancelled" com dois L; "ativa/futura" = pending|awaiting_payment|confirmed|seated)

Order:   (CONTÉM customer.name embutido; valores monetários em CENTAVOS)
  { id, clientId?, type:("DELIVERY"|"INDOOR"|"TAKEOUT"),
    status:("CREATED"|"PREPARING"|"READY_FOR_PICKUP"|"DISPATCHED"|"CONCLUDED"|"CANCELED"),
    customer:{ name, phone },
    items:[ { productId, name, quantity, unitPrice:centavos, total:centavos } ],
    subtotal:centavos, deliveryFee:centavos, discount:centavos, total:centavos, createdAt:ms }

Coupon:
  { id, name, description, type:("uniqueCode"|"singleCode"), status:("active"|"archived"),
    benefit:{ type:("FREE_TEXT"|...), text }, maxRedeems, maxUsages, createdAt:ms }

TopSpenderItem:  (retorno de clients_top_spenders.items)
  { client:{...Client}, spent:reais, couponsUsed }

AvailabilityResponse:  (retorno de reservations_availability)
  { date:"YYYY-MM-DD", totalCapacity,
    areas:[ { id, name, capacity } ],
    slots:[ { time:"HH:MM", capacity, booked, free } ] }

CancellationReasonCode ∈ ( client_changed_mind | client_schedule_conflict | client_request |
  no_show | no_availability | business_closed | duplicate_reservation | other )"""

CONVENTIONS = """CONVENÇÕES E UNIDADES:
- Timestamps: epoch em MILISSEGUNDOS (int). Datas de input: "YYYY-MM-DD".
- Dinheiro em CENTAVOS (divida por 100): Order (unitPrice, subtotal, total, deliveryFee,
  discount) e analytics (revenueCents, averageTicketCents, revenueCentsEstimate, byDay).
  Já 'spent' (top_spenders) e 'minSpent' estão em REAIS. Campos com sufixo "Cents" = centavos.
- "lugares/assentos disponíveis" = slots[].free (a mesma capacidade vale por horário;
  NUNCA conte a quantidade de slots nem some 'free' entre horários diferentes).
- Reservas NÃO têm nome do cliente. Para achar a reserva "do Fulano":
  clients_search(name) → pegue clientId → clients_reservations(clientId)
  (ou filtre reservas por clientId). Não filtre reservas por nome.
- Pedidos (Order) TÊM customer.name embutido — pode filtrar/agrupar por ele direto.
- Use SOMENTE os valores de enum listados acima (status, areaId, gender, reasonCode, coupon type).
  Ex.: reservations_create.areaId só aceita area_salao|area_varanda|area_mezanino.
- Ferramentas analytics_* e de configuração (delivery_get_config, store_get, store_get_hours,
  store_features) retornam um OBJETO de métricas/config — NÃO uma lista. Leia os campos
  diretamente na resposta; nunca aplique filter/find/project/count/dedup sobre elas.
  Ex.: "taxa de no-show" = chamar analytics_reservations e ler o campo da taxa no objeto."""


# ---------------------------------------------------------------------------
# CLIENTES (9)
# ---------------------------------------------------------------------------

clients_search = ToolDefinition(
    name="clients_search",
    domain="clients",
    destructive=False,
    description=(
        "Purpose: Find a specific client by name or phone number.\n"
        "Use when: The operator mentions a client by name ('a Ana', 'João Silva') or provides a phone number.\n"
        "Not when: Browsing or filtering a set of clients — use clients_list instead.\n"
        "vs clients_list: search is for INDIVIDUALS; list is for SETS.\n"
        "Returns: {total, items:[Client]}. If total > 1, always ask the operator to clarify before acting.\n"
        "Side effects: none (read-only).\n"
        "Anti-example: do NOT call search(name='') to list all clients — use clients_list."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Partial or full client name"},
            "phone": {"type": "string", "description": "Digits only"},
        },
    },
    method="GET",
    path_template="/api/case-mock/clients/search",
)

clients_list = ToolDefinition(
    name="clients_list",
    domain="clients",
    destructive=False,
    description=(
        "Purpose: List or paginate clients, optionally filtered by group.\n"
        "Use when: Browsing clients, filtering by group, or counting total clients.\n"
        "Not when: You know the client's name or phone — use clients_search instead.\n"
        "vs clients_search: list is for SETS; search is for INDIVIDUALS.\n"
        "Returns: {total, limit, offset, items:[Client]}. Use 'total' to count.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Search by name or phone"},
            "groupId": {"type": "string", "description": "Filter by group ID"},
            "limit": {"type": "integer", "default": 50},
            "offset": {"type": "integer", "default": 0},
        },
    },
    method="GET",
    path_template="/api/case-mock/clients",
)

clients_create = ToolDefinition(
    name="clients_create",
    domain="clients",
    destructive=False,
    description=(
        "Purpose: Register a new client in the store.\n"
        "Use when: The operator wants to add a new client.\n"
        "Not when: The client might already exist — search first to avoid duplicates (409 conflict).\n"
        "Returns: The created Client object.\n"
        "Side effects: Creates a permanent record."
    ),
    parameters_schema={
        "type": "object",
        "required": ["name", "phone"],
        "properties": {
            "name": {"type": "string"},
            "phone": {"type": "string", "description": "Digits only; unique per store"},
            "email": {"type": "string"},
            "cpf": {"type": "string"},
            "gender": {"type": "string", "enum": ["masc", "fem"]},
            "notes": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/clients",
)

clients_get = ToolDefinition(
    name="clients_get",
    domain="clients",
    destructive=False,
    description=(
        "Purpose: Fetch full details of a single client by ID.\n"
        "Use when: You already have a clientId and need full client data.\n"
        "Not when: You only have a name or phone — use clients_search first.\n"
        "Returns: Client object.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["clientId"],
        "properties": {
            "clientId": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/clients/{clientId}",
    path_params=["clientId"],
)

clients_update = ToolDefinition(
    name="clients_update",
    domain="clients",
    destructive=False,
    description=(
        "Purpose: Update one or more fields of an existing client.\n"
        "Use when: The operator wants to change client data (name, phone, notes, etc.).\n"
        "Returns: Updated Client object.\n"
        "Side effects: Persists changes to client record."
    ),
    parameters_schema={
        "type": "object",
        "required": ["clientId"],
        "properties": {
            "clientId": {"type": "string"},
            "name": {"type": "string"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "cpf": {"type": "string"},
            "gender": {"type": "string", "enum": ["masc", "fem"]},
            "notes": {"type": "string"},
        },
    },
    method="PATCH",
    path_template="/api/case-mock/clients/{clientId}",
    path_params=["clientId"],
)

clients_insights = ToolDefinition(
    name="clients_insights",
    domain="clients",
    destructive=False,
    description=(
        "Purpose: Get derived metrics for a client: visits, spend, no-shows, average ticket.\n"
        "Use when: The operator asks about a client's history, value, or behavior.\n"
        "Returns: {visitsCount, noShowCount, totalSpent, spentThisMonth, couponsUsed, firstVisitAt, lastVisitAt, averageTicket}.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["clientId"],
        "properties": {
            "clientId": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/clients/{clientId}/insights",
    path_params=["clientId"],
)

clients_reservations = ToolDefinition(
    name="clients_reservations",
    domain="clients",
    destructive=False,
    description=(
        "Purpose: List all reservations belonging to a specific client.\n"
        "Use when: You need to find a client's reservation(s) before acting on them.\n"
        "Not when: You want reservations across all clients — use reservations_list instead.\n"
        "Returns: {total, items:[Reservation]}.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["clientId"],
        "properties": {
            "clientId": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/clients/{clientId}/reservations",
    path_params=["clientId"],
)

clients_top_spenders = ToolDefinition(
    name="clients_top_spenders",
    domain="clients",
    destructive=False,
    description=(
        "Purpose: Rank clients by total spend in a given period.\n"
        "Use when: The operator asks who spends the most, top clients, or VIP ranking.\n"
        "Returns: {period, total, items:[{client, spent, couponsUsed}]} sorted by spend descending.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "period": {"type": "string", "enum": ["month", "all"], "default": "month"},
            "minSpent": {"type": "integer", "description": "Minimum spend in BRL"},
            "limit": {"type": "integer", "default": 20},
        },
    },
    method="GET",
    path_template="/api/case-mock/clients/top-spenders",
)

clients_inactive = ToolDefinition(
    name="clients_inactive",
    domain="clients",
    destructive=False,
    description=(
        "Purpose: List clients who have not visited in the last N days.\n"
        "Use when: The operator asks about inactive clients, churned customers, or reactivation targets.\n"
        "Returns: {days, total, items:[{client, lastVisitAt}]}.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "default": 60, "description": "Inactivity threshold in days"},
        },
    },
    method="GET",
    path_template="/api/case-mock/clients/inactive",
)

# ---------------------------------------------------------------------------
# RESERVAS (8)
# ---------------------------------------------------------------------------

reservations_list = ToolDefinition(
    name="reservations_list",
    domain="reservations",
    destructive=False,
    description=(
        "Purpose: List reservations filtered by date, status, or client.\n"
        "Use when: Querying reservations across clients, checking a day's bookings, or filtering by status.\n"
        "Not when: You need one client's reservations — use clients_reservations instead.\n"
        "Returns: {total, items:[Reservation]}.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "status": {"type": "string", "enum": ["pending", "awaiting_payment", "confirmed", "seated", "completed", "cancelled", "cancelled_by_client", "cancelled_by_business", "no_show"]},
            "clientId": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/reservations",
)

reservations_availability = ToolDefinition(
    name="reservations_availability",
    domain="reservations",
    destructive=False,
    description=(
        "Purpose: Check available slots and capacity for a given date.\n"
        "Use when: Before creating or rescheduling a reservation, or when the operator asks about availability.\n"
        "Returns: {date, totalCapacity, areas, slots:[{time, capacity, booked, free}]}.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD (default: today)"},
        },
    },
    method="GET",
    path_template="/api/case-mock/reservations/availability",
)

reservations_create = ToolDefinition(
    name="reservations_create",
    domain="reservations",
    destructive=False,
    description=(
        "Purpose: Create a new reservation for a client.\n"
        "Use when: The operator wants to book a table for a client.\n"
        "Tip: Check availability first with reservations_availability.\n"
        "Returns: Created Reservation (status: pending).\n"
        "Side effects: Creates a reservation record."
    ),
    parameters_schema={
        "type": "object",
        "required": ["clientId", "start", "adults"],
        "properties": {
            "clientId": {"type": "string"},
            "start": {"type": "integer", "description": "Start timestamp in milliseconds"},
            "adults": {"type": "integer", "minimum": 1},
            "children": {"type": "integer"},
            "areaId": {"type": "string", "enum": ["area_salao", "area_varanda", "area_mezanino"]},
            "description": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/reservations",
)

reservations_get = ToolDefinition(
    name="reservations_get",
    domain="reservations",
    destructive=False,
    description=(
        "Purpose: Fetch details of a single reservation by ID.\n"
        "Use when: You have a reservationId and need full details.\n"
        "Returns: Reservation object.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/reservations/{id}",
    path_params=["id"],
)

reservations_update = ToolDefinition(
    name="reservations_update",
    domain="reservations",
    destructive=False,
    description=(
        "Purpose: Update non-critical fields of a reservation (area, party size, table, notes).\n"
        "Use when: Changing seating area, party size or adding notes — NOT for rescheduling.\n"
        "Not when: Changing the date/time — use reservations_reschedule instead.\n"
        "Returns: Updated Reservation.\n"
        "Side effects: Persists changes."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
            "areaId": {"type": "string", "enum": ["area_salao", "area_varanda", "area_mezanino"]},
            "adults": {"type": "integer"},
            "children": {"type": "integer"},
            "tableId": {"type": "string"},
            "description": {"type": "string"},
        },
    },
    method="PATCH",
    path_template="/api/case-mock/reservations/{id}",
    path_params=["id"],
)

reservations_confirm = ToolDefinition(
    name="reservations_confirm",
    domain="reservations",
    destructive=False,
    description=(
        "Purpose: Confirm a pending reservation.\n"
        "Use when: The operator wants to confirm a reservation that is in 'pending' status.\n"
        "Returns: Reservation with status 'confirmed'.\n"
        "Side effects: Changes reservation status."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/reservations/{id}/confirm",
    path_params=["id"],
)

reservations_cancel = ToolDefinition(
    name="reservations_cancel",
    domain="reservations",
    destructive=True,
    description=(
        "⚠️ DESTRUCTIVE — IRREVERSIBLE.\n"
        "Purpose: Cancel a reservation permanently.\n"
        "Use when: The operator explicitly requests cancellation.\n"
        "Not when: The operator wants to change the time — use reservations_reschedule instead.\n"
        "Always confirm with the operator before executing.\n"
        "Returns: Reservation with status 'cancelled_by_business'.\n"
        "Side effects: Reservation is permanently cancelled. Cannot be undone."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
            "reasonCode": {"type": "string", "enum": ["client_changed_mind", "client_schedule_conflict", "client_request", "no_show", "no_availability", "business_closed", "duplicate_reservation", "other"]},
            "reasonText": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/reservations/{id}/cancel",
    path_params=["id"],
)

reservations_reschedule = ToolDefinition(
    name="reservations_reschedule",
    domain="reservations",
    destructive=True,
    description=(
        "⚠️ DESTRUCTIVE — IRREVERSIBLE.\n"
        "Purpose: Move a reservation to a new date/time.\n"
        "Use when: The operator explicitly requests rescheduling.\n"
        "Always confirm the new date/time with the operator before executing.\n"
        "Returns: Reservation with updated start time.\n"
        "Side effects: Original time slot is released and new slot is booked. Cannot be undone."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id", "start"],
        "properties": {
            "id": {"type": "string"},
            "start": {"type": "integer", "description": "New start timestamp in milliseconds"},
        },
    },
    method="POST",
    path_template="/api/case-mock/reservations/{id}/reschedule",
    path_params=["id"],
)

# ---------------------------------------------------------------------------
# PEDIDOS (6)
# ---------------------------------------------------------------------------

orders_list = ToolDefinition(
    name="orders_list",
    domain="orders",
    destructive=False,
    description=(
        "Purpose: List orders filtered by date, status, type, or client.\n"
        "Use when: Querying orders across the store or for a specific client.\n"
        "Returns: Paginated list of orders.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "status": {"type": "string"},
            "type": {"type": "string"},
            "clientId": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
            "offset": {"type": "integer", "default": 0},
        },
    },
    method="GET",
    path_template="/api/case-mock/orders",
)

orders_create = ToolDefinition(
    name="orders_create",
    domain="orders",
    destructive=False,
    description=(
        "Purpose: Create a new order for a client.\n"
        "Use when: The operator wants to register a new order.\n"
        "Returns: Created order object.\n"
        "Side effects: Creates an order record."
    ),
    parameters_schema={
        "type": "object",
        "required": ["items", "type", "clientId", "paymentMethod"],
        "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {"productId": {"type": "string"}, "quantity": {"type": "integer"}}}},
            "type": {"type": "string"},
            "clientId": {"type": "string"},
            "paymentMethod": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/orders",
)

orders_stats = ToolDefinition(
    name="orders_stats",
    domain="orders",
    destructive=False,
    description=(
        "Purpose: Get order statistics for a time period.\n"
        "Use when: The operator asks about order volume, totals, or trends in a period.\n"
        "Returns: Aggregated order metrics.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "periodStart": {"type": "integer", "description": "Start timestamp in ms"},
            "periodEnd": {"type": "integer", "description": "End timestamp in ms"},
        },
    },
    method="GET",
    path_template="/api/case-mock/orders/stats",
)

orders_get = ToolDefinition(
    name="orders_get",
    domain="orders",
    destructive=False,
    description=(
        "Purpose: Fetch details of a single order by ID.\n"
        "Use when: You have an orderId and need full order details.\n"
        "Returns: Order object.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/orders/{id}",
    path_params=["id"],
)

orders_update_status = ToolDefinition(
    name="orders_update_status",
    domain="orders",
    destructive=False,
    description=(
        "Purpose: Update the status of an order.\n"
        "Use when: The operator wants to advance or change an order's status.\n"
        "Not when: The operator wants to cancel — use orders_cancel instead.\n"
        "Returns: Updated order.\n"
        "Side effects: Changes order status."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id", "status"],
        "properties": {
            "id": {"type": "string"},
            "status": {"type": "string"},
        },
    },
    method="PATCH",
    path_template="/api/case-mock/orders/{id}/status",
    path_params=["id"],
)

orders_cancel = ToolDefinition(
    name="orders_cancel",
    domain="orders",
    destructive=True,
    description=(
        "⚠️ DESTRUCTIVE — IRREVERSIBLE.\n"
        "Purpose: Cancel an order permanently.\n"
        "Use when: The operator explicitly requests order cancellation.\n"
        "Always confirm with the operator before executing.\n"
        "Returns: Cancelled order.\n"
        "Side effects: Order is permanently cancelled. Cannot be undone."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/orders/{id}/cancel",
    path_params=["id"],
)

# ---------------------------------------------------------------------------
# CUPONS (8)
# ---------------------------------------------------------------------------

coupons_list = ToolDefinition(
    name="coupons_list",
    domain="coupons",
    destructive=False,
    description=(
        "Purpose: List coupons, optionally filtered by status.\n"
        "Use when: The operator wants to see available, used, or all coupons.\n"
        "Returns: List of coupon objects.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/coupons",
)

coupons_create = ToolDefinition(
    name="coupons_create",
    domain="coupons",
    destructive=False,
    description=(
        "Purpose: Create a new coupon.\n"
        "Use when: The operator wants to create a discount or benefit coupon.\n"
        "Returns: Created coupon.\n"
        "Side effects: Creates a coupon record."
    ),
    parameters_schema={
        "type": "object",
        "required": ["name", "type", "benefitText"],
        "properties": {
            "name": {"type": "string"},
            "type": {"type": "string"},
            "benefitText": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/coupons",
)

coupons_get = ToolDefinition(
    name="coupons_get",
    domain="coupons",
    destructive=False,
    description=(
        "Purpose: Fetch details of a single coupon by ID.\n"
        "Use when: You have a couponId and need full coupon data.\n"
        "Returns: Coupon object.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/coupons/{id}",
    path_params=["id"],
)

coupons_update = ToolDefinition(
    name="coupons_update",
    domain="coupons",
    destructive=False,
    description=(
        "Purpose: Update fields of an existing coupon.\n"
        "Use when: The operator wants to modify a coupon's properties.\n"
        "Returns: Updated coupon.\n"
        "Side effects: Persists changes."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "benefitText": {"type": "string"},
        },
    },
    method="PATCH",
    path_template="/api/case-mock/coupons/{id}",
    path_params=["id"],
)

coupons_analytics = ToolDefinition(
    name="coupons_analytics",
    domain="coupons",
    destructive=False,
    description=(
        "Purpose: Get usage metrics for a coupon (generated, used, usage rate).\n"
        "Use when: The operator asks about coupon performance.\n"
        "Returns: {generated, used, usageRate}.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/coupons/{id}/analytics",
    path_params=["id"],
)

coupons_instances = ToolDefinition(
    name="coupons_instances",
    domain="coupons",
    destructive=False,
    description=(
        "Purpose: List individual coupon codes/instances for a coupon.\n"
        "Use when: The operator wants to see specific coupon codes or filter by state.\n"
        "Returns: List of coupon instances.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
            "state": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/coupons/{id}/instances",
    path_params=["id"],
)

coupons_assign_group = ToolDefinition(
    name="coupons_assign_group",
    domain="coupons",
    destructive=False,
    description=(
        "Purpose: Generate a coupon instance for every client in a group.\n"
        "Use when: The operator wants to distribute a coupon to a client group (reactivation campaign, etc.).\n"
        "Returns: Summary of generated instances.\n"
        "Side effects: Creates coupon instances for all group members."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id", "groupId"],
        "properties": {
            "id": {"type": "string"},
            "groupId": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/coupons/{id}/assign-group",
    path_params=["id"],
)

coupons_deactivate = ToolDefinition(
    name="coupons_deactivate",
    domain="coupons",
    destructive=True,
    description=(
        "⚠️ DESTRUCTIVE — IRREVERSIBLE.\n"
        "Purpose: Permanently deactivate a coupon. It can no longer be used.\n"
        "Use when: The operator explicitly wants to disable a coupon.\n"
        "Always confirm with the operator before executing.\n"
        "Side effects: Coupon is permanently deactivated. Cannot be re-activated."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/coupons/{id}/deactivate",
    path_params=["id"],
)

# ---------------------------------------------------------------------------
# PROMOÇÕES (6)
# ---------------------------------------------------------------------------

promotions_list = ToolDefinition(
    name="promotions_list",
    domain="promotions",
    destructive=False,
    description=(
        "Purpose: List promotions, optionally filtered by status or discount type.\n"
        "Use when: The operator wants to see active, inactive, or all promotions.\n"
        "Returns: List of promotion objects.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "discountType": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/promotions",
)

promotions_create = ToolDefinition(
    name="promotions_create",
    domain="promotions",
    destructive=False,
    description=(
        "Purpose: Create a new promotion.\n"
        "Use when: The operator wants to add a new discount or offer.\n"
        "Returns: Created promotion.\n"
        "Side effects: Creates a promotion record."
    ),
    parameters_schema={
        "type": "object",
        "required": ["name", "discountType", "discountValue", "validFrom", "validUntil"],
        "properties": {
            "name": {"type": "string"},
            "discountType": {"type": "string"},
            "discountValue": {"type": "number"},
            "validFrom": {"type": "integer", "description": "Timestamp in ms"},
            "validUntil": {"type": "integer", "description": "Timestamp in ms"},
        },
    },
    method="POST",
    path_template="/api/case-mock/promotions",
)

promotions_get = ToolDefinition(
    name="promotions_get",
    domain="promotions",
    destructive=False,
    description=(
        "Purpose: Fetch details of a single promotion by ID.\n"
        "Use when: You have a promotionId and need full details.\n"
        "Returns: Promotion object.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/promotions/{id}",
    path_params=["id"],
)

promotions_update = ToolDefinition(
    name="promotions_update",
    domain="promotions",
    destructive=False,
    description=(
        "Purpose: Update fields of an existing promotion.\n"
        "Use when: The operator wants to modify a promotion's name, dates, or discount.\n"
        "Returns: Updated promotion.\n"
        "Side effects: Persists changes."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "discountType": {"type": "string"},
            "discountValue": {"type": "number"},
            "validFrom": {"type": "integer"},
            "validUntil": {"type": "integer"},
        },
    },
    method="PUT",
    path_template="/api/case-mock/promotions/{id}",
    path_params=["id"],
)

promotions_analytics = ToolDefinition(
    name="promotions_analytics",
    domain="promotions",
    destructive=False,
    description=(
        "Purpose: Get usage metrics for a promotion.\n"
        "Use when: The operator asks about promotion performance.\n"
        "Returns: Promotion usage metrics.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/promotions/{id}/analytics",
    path_params=["id"],
)

promotions_delete = ToolDefinition(
    name="promotions_delete",
    domain="promotions",
    destructive=True,
    description=(
        "⚠️ DESTRUCTIVE — IRREVERSIBLE.\n"
        "Purpose: Permanently delete a promotion.\n"
        "Use when: The operator explicitly wants to remove a promotion.\n"
        "Not when: The operator just wants to disable it — consider updating validUntil instead.\n"
        "Always confirm with the operator before executing.\n"
        "Side effects: Promotion is permanently deleted. Cannot be recovered."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="DELETE",
    path_template="/api/case-mock/promotions/{id}",
    path_params=["id"],
)

# ---------------------------------------------------------------------------
# DELIVERY (6)
# ---------------------------------------------------------------------------

delivery_get_config = ToolDefinition(
    name="delivery_get_config",
    domain="delivery",
    destructive=False,
    description=(
        "Purpose: Get current delivery configuration (fees, minimum order, scheduling).\n"
        "Use when: The operator asks about delivery settings.\n"
        "Returns: Delivery config object.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={"type": "object", "properties": {}},
    method="GET",
    path_template="/api/case-mock/delivery/config",
)

delivery_update_config = ToolDefinition(
    name="delivery_update_config",
    domain="delivery",
    destructive=False,
    description=(
        "Purpose: Update delivery configuration.\n"
        "Use when: The operator wants to change delivery fees, minimum order, or scheduling settings.\n"
        "Returns: Updated delivery config.\n"
        "Side effects: Changes delivery settings for the store."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "minimumOrder": {"type": "number"},
            "deliveryFee": {"type": "number"},
            "estimatedTime": {"type": "integer"},
        },
    },
    method="PUT",
    path_template="/api/case-mock/delivery/config",
)

delivery_neighborhoods = ToolDefinition(
    name="delivery_neighborhoods",
    domain="delivery",
    destructive=False,
    description=(
        "Purpose: List neighborhoods served and their delivery fees.\n"
        "Use when: The operator asks which areas are covered or what fees apply.\n"
        "Returns: List of neighborhoods with fees.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={"type": "object", "properties": {}},
    method="GET",
    path_template="/api/case-mock/delivery/neighborhoods",
)

delivery_current_pause = ToolDefinition(
    name="delivery_current_pause",
    domain="delivery",
    destructive=False,
    description=(
        "Purpose: Check if delivery is currently paused.\n"
        "Use when: The operator asks about current delivery status.\n"
        "Returns: Active pause object or null.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={"type": "object", "properties": {}},
    method="GET",
    path_template="/api/case-mock/delivery/pauses/current",
)

delivery_create_pause = ToolDefinition(
    name="delivery_create_pause",
    domain="delivery",
    destructive=True,
    description=(
        "⚠️ DESTRUCTIVE — Interrupts all incoming delivery orders.\n"
        "Purpose: Pause delivery operations for the store.\n"
        "Use when: The operator explicitly wants to stop accepting delivery orders.\n"
        "Safe alternative: If unsure, suggest ending the pause instead of creating one.\n"
        "Always confirm with the operator before executing.\n"
        "Side effects: No new delivery orders will be accepted until the pause is ended."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "durationMinutes": {"type": "integer"},
        },
    },
    method="POST",
    path_template="/api/case-mock/delivery/pauses",
)

delivery_end_pause = ToolDefinition(
    name="delivery_end_pause",
    domain="delivery",
    destructive=False,
    description=(
        "Purpose: End an active delivery pause and resume accepting orders.\n"
        "Use when: The operator wants to resume delivery after a pause.\n"
        "Returns: Ended pause object.\n"
        "Side effects: Delivery resumes."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="DELETE",
    path_template="/api/case-mock/delivery/pauses/{id}",
    path_params=["id"],
)

# ---------------------------------------------------------------------------
# IFOOD (6)
# ---------------------------------------------------------------------------

ifood_merchants = ToolDefinition(
    name="ifood_merchants",
    domain="ifood",
    destructive=False,
    description=(
        "Purpose: List iFood merchant accounts linked to the store.\n"
        "Use when: The operator asks about iFood integration or merchant IDs.\n"
        "Returns: List of iFood merchant objects.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={"type": "object", "properties": {}},
    method="GET",
    path_template="/api/case-mock/ifood/merchants",
)

ifood_list = ToolDefinition(
    name="ifood_list",
    domain="ifood",
    destructive=False,
    description=(
        "Purpose: List iFood orders filtered by date, status, or pagination.\n"
        "Use when: The operator asks about iFood orders.\n"
        "Returns: Paginated list of iFood orders.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "status": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
            "offset": {"type": "integer", "default": 0},
        },
    },
    method="GET",
    path_template="/api/case-mock/ifood/orders",
)

ifood_get = ToolDefinition(
    name="ifood_get",
    domain="ifood",
    destructive=False,
    description=(
        "Purpose: Fetch details of a single iFood order by ID.\n"
        "Use when: You have an iFood order ID and need full details.\n"
        "Returns: iFood order object.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="GET",
    path_template="/api/case-mock/ifood/orders/{id}",
    path_params=["id"],
)

ifood_confirm = ToolDefinition(
    name="ifood_confirm",
    domain="ifood",
    destructive=False,
    description=(
        "Purpose: Confirm a pending iFood order (PENDING → CONFIRMED).\n"
        "Use when: The operator wants to accept an iFood order.\n"
        "Returns: Updated iFood order.\n"
        "Side effects: Order is confirmed."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/ifood/orders/{id}/confirm",
    path_params=["id"],
)

ifood_dispatch = ToolDefinition(
    name="ifood_dispatch",
    domain="ifood",
    destructive=False,
    description=(
        "Purpose: Dispatch a confirmed iFood order for delivery.\n"
        "Use when: The operator wants to mark an order as out for delivery.\n"
        "Returns: Updated iFood order.\n"
        "Side effects: Order status advances to dispatched."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/ifood/orders/{id}/dispatch",
    path_params=["id"],
)

ifood_cancel = ToolDefinition(
    name="ifood_cancel",
    domain="ifood",
    destructive=True,
    description=(
        "⚠️ DESTRUCTIVE — IRREVERSIBLE.\n"
        "Purpose: Request cancellation of an iFood order.\n"
        "Use when: The operator explicitly wants to cancel an iFood order.\n"
        "Always confirm with the operator before executing.\n"
        "Side effects: Cancellation is permanent and reported to iFood. Cannot be undone."
    ),
    parameters_schema={
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string"},
        },
    },
    method="POST",
    path_template="/api/case-mock/ifood/orders/{id}/cancel",
    path_params=["id"],
)

# ---------------------------------------------------------------------------
# LOJA (6)
# ---------------------------------------------------------------------------

store_get = ToolDefinition(
    name="store_get",
    domain="store",
    destructive=False,
    description=(
        "Purpose: Get store data (name, address, plan, payment methods).\n"
        "Use when: The operator asks about store settings or info.\n"
        "Returns: Store object.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={"type": "object", "properties": {}},
    method="GET",
    path_template="/api/case-mock/store",
)

store_update = ToolDefinition(
    name="store_update",
    domain="store",
    destructive=False,
    description=(
        "Purpose: Update store data.\n"
        "Use when: The operator wants to change store name, address, or other settings.\n"
        "Returns: Updated store object.\n"
        "Side effects: Persists changes to store settings."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "address": {"type": "string"},
        },
    },
    method="PUT",
    path_template="/api/case-mock/store",
)

store_get_hours = ToolDefinition(
    name="store_get_hours",
    domain="store",
    destructive=False,
    description=(
        "Purpose: Get store working hours per day of week.\n"
        "Use when: The operator asks about opening hours.\n"
        "Returns: Working hours by day.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={"type": "object", "properties": {}},
    method="GET",
    path_template="/api/case-mock/store/hours",
)

store_update_hours = ToolDefinition(
    name="store_update_hours",
    domain="store",
    destructive=False,
    description=(
        "Purpose: Update store working hours.\n"
        "Use when: The operator wants to change opening/closing times.\n"
        "Returns: Updated working hours.\n"
        "Side effects: Changes store hours."
    ),
    parameters_schema={
        "type": "object",
        "required": ["workingHours"],
        "properties": {
            "workingHours": {"type": "object", "description": "Map of day to hours"},
        },
    },
    method="PUT",
    path_template="/api/case-mock/store/hours",
)

store_members = ToolDefinition(
    name="store_members",
    domain="store",
    destructive=False,
    description=(
        "Purpose: List team members and their roles.\n"
        "Use when: The operator asks about staff or team.\n"
        "Returns: List of members with roles.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={"type": "object", "properties": {}},
    method="GET",
    path_template="/api/case-mock/store/members",
)

store_features = ToolDefinition(
    name="store_features",
    domain="store",
    destructive=False,
    description=(
        "Purpose: List active feature flags and integrations for the store.\n"
        "Use when: The operator asks what features or integrations are enabled.\n"
        "Returns: Feature flags object.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={"type": "object", "properties": {}},
    method="GET",
    path_template="/api/case-mock/store/features",
)

# ---------------------------------------------------------------------------
# ANALYTICS (6)
# ---------------------------------------------------------------------------

analytics_revenue = ToolDefinition(
    name="analytics_revenue",
    domain="analytics",
    destructive=False,
    description=(
        "Purpose: Get revenue for a period, broken down by day.\n"
        "Use when: The operator asks about revenue, faturamento, or financial performance.\n"
        "Returns: {total, byDay:[{date, revenue}]}.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "periodStart": {"type": "integer", "description": "Start timestamp in ms"},
            "periodEnd": {"type": "integer", "description": "End timestamp in ms"},
        },
    },
    method="GET",
    path_template="/api/case-mock/analytics/revenue",
)

analytics_orders = ToolDefinition(
    name="analytics_orders",
    domain="analytics",
    destructive=False,
    description=(
        "Purpose: Get order metrics for a period.\n"
        "Use when: The operator asks about order volume, averages, or trends.\n"
        "Returns: Aggregated order analytics.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "periodStart": {"type": "integer", "description": "Start timestamp in ms"},
            "periodEnd": {"type": "integer", "description": "End timestamp in ms"},
        },
    },
    method="GET",
    path_template="/api/case-mock/analytics/orders",
)

analytics_reservations = ToolDefinition(
    name="analytics_reservations",
    domain="analytics",
    destructive=False,
    description=(
        "Purpose: Get reservation metrics for a period, including no-show rate.\n"
        "Use when: The operator asks about reservation performance or no-shows.\n"
        "Returns: Reservation analytics including no-show rate.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "periodStart": {"type": "integer", "description": "Start timestamp in ms"},
            "periodEnd": {"type": "integer", "description": "End timestamp in ms"},
        },
    },
    method="GET",
    path_template="/api/case-mock/analytics/reservations",
)

analytics_conversations = ToolDefinition(
    name="analytics_conversations",
    domain="analytics",
    destructive=False,
    description=(
        "Purpose: Get conversation metrics (AI vs human handled) for a period.\n"
        "Use when: The operator asks about WhatsApp conversation performance.\n"
        "Returns: Conversation metrics by type.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "periodStart": {"type": "integer", "description": "Start timestamp in ms"},
            "periodEnd": {"type": "integer", "description": "End timestamp in ms"},
        },
    },
    method="GET",
    path_template="/api/case-mock/analytics/conversations",
)

analytics_coupon_returns = ToolDefinition(
    name="analytics_coupon_returns",
    domain="analytics",
    destructive=False,
    description=(
        "Purpose: Get coupon return metrics for a period (generated vs used).\n"
        "Use when: The operator asks about coupon campaign effectiveness.\n"
        "Returns: {generated, used, returnRate}.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "periodStart": {"type": "integer", "description": "Start timestamp in ms"},
            "periodEnd": {"type": "integer", "description": "End timestamp in ms"},
        },
    },
    method="GET",
    path_template="/api/case-mock/analytics/coupon-returns",
)

analytics_top_items = ToolDefinition(
    name="analytics_top_items",
    domain="analytics",
    destructive=False,
    description=(
        "Purpose: Get best-selling items for a period.\n"
        "Use when: The operator asks about popular dishes, top products, or menu performance.\n"
        "Returns: Ranked list of items with sales count.\n"
        "Side effects: none (read-only)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "periodStart": {"type": "integer", "description": "Start timestamp in ms"},
            "periodEnd": {"type": "integer", "description": "End timestamp in ms"},
            "limit": {"type": "integer", "default": 10},
        },
    },
    method="GET",
    path_template="/api/case-mock/analytics/top-items",
)

# ---------------------------------------------------------------------------
# Master list
# ---------------------------------------------------------------------------

ALL_TOOLS: list[ToolDefinition] = [
    clients_search, clients_list, clients_create, clients_get, clients_update,
    clients_insights, clients_reservations, clients_top_spenders, clients_inactive,
    reservations_list, reservations_availability, reservations_create, reservations_get,
    reservations_update, reservations_confirm, reservations_cancel, reservations_reschedule,
    orders_list, orders_create, orders_stats, orders_get, orders_update_status, orders_cancel,
    coupons_list, coupons_create, coupons_get, coupons_update, coupons_analytics,
    coupons_instances, coupons_assign_group, coupons_deactivate,
    promotions_list, promotions_create, promotions_get, promotions_update,
    promotions_analytics, promotions_delete,
    delivery_get_config, delivery_update_config, delivery_neighborhoods,
    delivery_current_pause, delivery_create_pause, delivery_end_pause,
    ifood_merchants, ifood_list, ifood_get, ifood_confirm, ifood_dispatch, ifood_cancel,
    store_get, store_update, store_get_hours, store_update_hours, store_members, store_features,
    analytics_revenue, analytics_orders, analytics_reservations, analytics_conversations,
    analytics_coupon_returns, analytics_top_items,
]


# ---------------------------------------------------------------------------
# Return shapes — what each tool returns, so the planner references real fields
# (see DATA_MODEL for the entity shapes). Confident shapes come from the OpenAPI
# contract and live dumps; where a domain wasn't dumped yet the shape is marked
# "(forma exata: rode probe)" rather than invented.
# ---------------------------------------------------------------------------

_RETURNS: dict[str, str] = {
    # clients
    "clients_search":       "{ total, items:[Client] }  (se total>1, peça desambiguação)",
    "clients_list":         "{ total, limit, offset, items:[Client] }",
    "clients_create":       "Client (criado)",
    "clients_get":          "Client",
    "clients_update":       "Client (atualizado)",
    "clients_insights":     "ClientInsights",
    "clients_reservations": "{ total, items:[Reservation] }",
    "clients_top_spenders": "{ period, minSpent, total, items:[TopSpenderItem] }",
    "clients_inactive":     "{ days, total, items:[{ client:Client, lastVisitAt:ms? }] }",
    # reservations
    "reservations_list":         "{ total, items:[Reservation] }",
    "reservations_availability": "AvailabilityResponse { date, totalCapacity, areas:[{id,name,capacity}], slots:[{time,capacity,booked,free}] }",
    "reservations_create":       "Reservation (status pending)",
    "reservations_get":          "Reservation",
    "reservations_update":       "Reservation (atualizada)",
    "reservations_confirm":      "Reservation (status confirmed)",
    "reservations_cancel":       "Reservation (status cancelled)",
    "reservations_reschedule":   "Reservation (com novo duration.start)",
    # orders
    "orders_list":          "{ total, limit, offset, items:[Order] }",
    "orders_create":        "Order (criado)",
    "orders_stats":         "métricas de pedidos no período (forma exata: rode probe orders_stats)",
    "orders_get":           "Order",
    "orders_update_status": "Order (status atualizado)",
    "orders_cancel":        "Order (status CANCELED)",
    # coupons
    "coupons_list":         "{ total, items:[Coupon] }",
    "coupons_create":       "Coupon (criado)",
    "coupons_get":          "Coupon",
    "coupons_update":       "Coupon (atualizado)",
    "coupons_analytics":    "{ gerados, usados, taxa de uso } (forma exata: rode probe)",
    "coupons_instances":    "{ items:[{ ...instância/código, state }] } (forma exata: rode probe)",
    "coupons_assign_group": "{ ...instâncias geradas para cada cliente do grupo } (forma exata: rode probe)",
    "coupons_deactivate":   "Coupon (status archived)",
    # promotions
    "promotions_list":      "{ total, items:[Promotion] }  (forma de Promotion: rode probe)",
    "promotions_create":    "Promotion (criada)",
    "promotions_get":       "Promotion",
    "promotions_update":    "Promotion (atualizada)",
    "promotions_analytics": "métricas de uso da promoção (forma exata: rode probe)",
    "promotions_delete":    "{ ok } (remoção)",
    # delivery
    "delivery_get_config":    "DeliveryConfig { storeId, distanceCalculationType, minimumOrderValue, freeDeliveryThreshold, allowDelivery, allowTakeout, autoAcceptOrders, scheduling, updatedAt }",
    "delivery_update_config": "DeliveryConfig (atualizado)",
    "delivery_neighborhoods": "{ items:[{ bairro, taxa }] } (forma exata: rode probe)",
    "delivery_current_pause": "pausa ativa { id, reason, ... } ou vazio (forma exata: rode probe)",
    "delivery_create_pause":  "pausa criada { id, ... }",
    "delivery_end_pause":     "{ ok }",
    # ifood
    "ifood_merchants": "{ items:[merchant] } (forma exata: rode probe)",
    "ifood_list":      "{ ...pedidos iFood } (forma exata: rode probe ifood_list)",
    "ifood_get":       "pedido iFood (forma exata: rode probe)",
    "ifood_confirm":   "pedido iFood (status CONFIRMED)",
    "ifood_dispatch":  "pedido iFood (despachado)",
    "ifood_cancel":    "pedido iFood (cancelamento solicitado)",
    # store
    "store_get":          "dados da loja { nome, endereço, plano, pagamentos } (forma exata: rode probe)",
    "store_update":       "dados da loja (atualizados)",
    "store_get_hours":    "{ workingHours por dia } (forma exata: rode probe)",
    "store_update_hours": "{ workingHours atualizados }",
    "store_members":      "{ items:[membro/equipe com papel] } (forma exata: rode probe)",
    "store_features":     "{ feature flags / integrações ativas } (forma exata: rode probe)",
    # analytics (todas por período periodStart/periodEnd em ms; dinheiro em CENTAVOS)
    "analytics_revenue":       "{ periodStart, periodEnd, revenueCents, orderCount, averageTicketCents, byDay:{ 'DD/MM/YYYY': cents } }",
    "analytics_orders":        "{ periodStart, periodEnd, totalOrders, byType:{DELIVERY,INDOOR,TAKEOUT:int}, byStatus:{CREATED,PREPARING,READY_FOR_PICKUP,DISPATCHED,CONCLUDED,...:int} }",
    "analytics_reservations":  "{ periodStart, periodEnd, totalReservations, byStatus:{status:int}, noShowCount, noShowRate }",
    "analytics_conversations": "{ periodStart, periodEnd, totalHandled, aiHandled, humanHandled, transferToHuman, aiResolutionRate(%) }",
    "analytics_coupon_returns":"{ periodStart, periodEnd, generatedCount, usedCount, usedPercentOfGenerated(%), revenueCentsEstimate }",
    "analytics_top_items":     "{ periodStart, periodEnd, items:[{ productId, name, quantity, revenueCents }] }",
}

for _t in ALL_TOOLS:
    if _t.name in _RETURNS:
        _t.returns = _RETURNS[_t.name]
