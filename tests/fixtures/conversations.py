"""Realistic multi-turn conversation builders for integration testing.

Generates OpenAI-compatible message arrays that simulate real coding agent
sessions (Claude Code / OpenCode style). Each builder produces a list[dict]
ready for the /v1/chat/completions ``messages`` field.

Conversations include:
- System prompts with tool definitions
- Multi-turn user/assistant exchanges
- Tool call/result pairs (Read, Edit, Bash, Grep, Glob)
- Realistic code content at scale (50K+ tokens when needed)

Usage::

    from tests.fixtures.conversations import (
        build_coding_session_short,
        build_coding_session_long,
        build_recall_trigger_session,
        build_read_cache_session,
    )

    messages = build_coding_session_long()
    # → ~30 turns, ~60K tokens, exercises context assembly
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

# ---------------------------------------------------------------------------
# Tool call ID generator
# ---------------------------------------------------------------------------

_counter = 0


def _next_call_id() -> str:
    global _counter
    _counter += 1
    return f"call_{_counter:04d}"


def _reset_counter():
    global _counter
    _counter = 0


# ---------------------------------------------------------------------------
# System prompt (realistic Claude Code style)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = textwrap.dedent("""\
You are an AI coding assistant working on a Python web application.
You have access to the following tools:

- Read: Read a file from the filesystem. Parameters: file_path (string, required), offset (int, optional), limit (int, optional).
- Edit: Make targeted edits to a file. Parameters: file_path (string), old_string (string), new_string (string).
- Write: Write content to a file. Parameters: file_path (string), content (string).
- Bash: Execute a bash command. Parameters: command (string).
- Grep: Search for patterns in files. Parameters: pattern (string), path (string, optional), glob (string, optional).
- Glob: Find files matching a pattern. Parameters: pattern (string), path (string, optional).

When you need to use a tool, include a tool_calls array in your response.
Always read files before editing them. Run tests after making changes.
Be precise about file paths. Use absolute paths.

Project: /workspace/myapp — a FastAPI application with SQLAlchemy ORM,
Alembic migrations, pytest test suite, and Docker deployment.
""")


# ---------------------------------------------------------------------------
# Realistic code content for file reads
# ---------------------------------------------------------------------------

_MODELS_PY = textwrap.dedent("""\
\"\"\"SQLAlchemy ORM models for the application.\"\"\"

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Boolean, Float
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="user", cascade="all, delete-orphan")
    reviews: Mapped[list["Review"]] = relationship("Review", back_populates="user")
    addresses: Mapped[list["Address"]] = relationship("Address", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email={self.email!r})>"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    sku: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)
    category_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("categories.id"))
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    weight_kg: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    category: Mapped[Optional["Category"]] = relationship("Category", back_populates="products")
    order_items: Mapped[list["OrderItem"]] = relationship("OrderItem", back_populates="product")
    reviews: Mapped[list["Review"]] = relationship("Review", back_populates="product")

    @property
    def in_stock(self) -> bool:
        return self.stock_quantity > 0 and self.is_available

    def __repr__(self) -> str:
        return f"<Product(id={self.id}, name={self.name!r}, sku={self.sku!r})>"


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    parent_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("categories.id"))

    products: Mapped[list["Product"]] = relationship("Product", back_populates="category")
    children: Mapped[list["Category"]] = relationship("Category", back_populates="parent")
    parent: Mapped[Optional["Category"]] = relationship("Category", remote_side=[id], back_populates="children")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, confirmed, shipped, delivered, cancelled
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    shipping_address_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("addresses.id"))
    tracking_number: Mapped[Optional[str]] = mapped_column(String(100))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    shipping_address: Mapped[Optional["Address"]] = relationship("Address")

    @property
    def item_count(self) -> int:
        return sum(item.quantity for item in self.items)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="items")
    product: Mapped["Product"] = relationship("Product", back_populates="order_items")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5
    title: Mapped[Optional[str]] = mapped_column(String(200))
    body: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship("User", back_populates="reviews")
    product: Mapped["Product"] = relationship("Product", back_populates="reviews")


class Address(Base):
    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(50), default="home")  # home, work, other
    street: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[Optional[str]] = mapped_column(String(100))
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str] = mapped_column(String(100), default="US")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship("User", back_populates="addresses")
""")

_ROUTES_PY = textwrap.dedent("""\
\"\"\"FastAPI route handlers for the orders API.\"\"\"

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from app.database import get_db
from app.models import Order, OrderItem, Product, User
from app.schemas import OrderCreate, OrderResponse, OrderListResponse, OrderUpdate
from app.auth import get_current_user

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("/", response_model=OrderListResponse)
async def list_orders(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    status: Optional[str] = Query(None, description="Filter by order status"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    \"\"\"List orders for the current user with pagination.\"\"\"
    query = select(Order).where(Order.user_id == user.id)
    if status:
        query = query.where(Order.status == status)
    query = query.order_by(Order.created_at.desc())

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar()

    # Paginate
    query = query.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    orders = result.scalars().all()

    return OrderListResponse(
        orders=[OrderResponse.model_validate(o) for o in orders],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post("/", response_model=OrderResponse, status_code=201)
async def create_order(
    order_data: OrderCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    \"\"\"Create a new order from the user's cart items.\"\"\"
    # Validate all products exist and are in stock
    total = 0.0
    items = []
    for item in order_data.items:
        product = await db.get(Product, item.product_id)
        if not product:
            raise HTTPException(404, f"Product {item.product_id} not found")
        if product.stock_quantity < item.quantity:
            raise HTTPException(400, f"Insufficient stock for {product.name}")
        total += product.price * item.quantity
        items.append(OrderItem(
            product_id=item.product_id,
            quantity=item.quantity,
            unit_price=product.price,
        ))

    order = Order(
        user_id=user.id,
        total_amount=total,
        shipping_address_id=order_data.shipping_address_id,
        notes=order_data.notes,
    )
    order.items = items
    db.add(order)

    # Decrement stock
    for item in order_data.items:
        product = await db.get(Product, item.product_id)
        product.stock_quantity -= item.quantity

    await db.commit()
    await db.refresh(order)
    return OrderResponse.model_validate(order)


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    \"\"\"Get a specific order by ID.\"\"\"
    order = await db.get(Order, order_id)
    if not order or order.user_id != user.id:
        raise HTTPException(404, "Order not found")
    return OrderResponse.model_validate(order)


@router.patch("/{order_id}", response_model=OrderResponse)
async def update_order(
    order_id: int,
    update: OrderUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    \"\"\"Update order status or notes.\"\"\"
    order = await db.get(Order, order_id)
    if not order or order.user_id != user.id:
        raise HTTPException(404, "Order not found")

    # Status transition validation
    valid_transitions = {
        "pending": ["confirmed", "cancelled"],
        "confirmed": ["shipped", "cancelled"],
        "shipped": ["delivered"],
    }

    if update.status:
        allowed = valid_transitions.get(order.status, [])
        if update.status not in allowed:
            raise HTTPException(
                400,
                f"Cannot transition from '{order.status}' to '{update.status}'. "
                f"Allowed: {allowed}"
            )
        order.status = update.status

        # Restore stock on cancellation
        if update.status == "cancelled":
            for item in order.items:
                product = await db.get(Product, item.product_id)
                product.stock_quantity += item.quantity

    if update.tracking_number is not None:
        order.tracking_number = update.tracking_number
    if update.notes is not None:
        order.notes = update.notes

    await db.commit()
    await db.refresh(order)
    return OrderResponse.model_validate(order)


@router.delete("/{order_id}", status_code=204)
async def cancel_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    \"\"\"Cancel a pending order and restore stock.\"\"\"
    order = await db.get(Order, order_id)
    if not order or order.user_id != user.id:
        raise HTTPException(404, "Order not found")
    if order.status != "pending":
        raise HTTPException(400, "Only pending orders can be cancelled")

    order.status = "cancelled"
    for item in order.items:
        product = await db.get(Product, item.product_id)
        product.stock_quantity += item.quantity

    await db.commit()
""")

_CONFTEST_PY = textwrap.dedent("""\
\"\"\"Pytest fixtures for the application test suite.\"\"\"

import asyncio
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from app.models import Base
from app.database import get_db

TEST_DATABASE_URL = "sqlite+aiosqlite:///./test.db"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
""")

_TEST_ORDERS_PY = textwrap.dedent("""\
\"\"\"Tests for the orders API endpoints.\"\"\"

import pytest
from httpx import AsyncClient

from app.models import User, Product, Category, Order


@pytest.mark.asyncio
async def test_create_order_success(client: AsyncClient, db_session):
    # Setup: create user, product, and authenticate
    user = User(email="test@example.com", username="testuser", hashed_password="hash")
    product = Product(name="Widget", sku="WDG-001", price=29.99, stock_quantity=100)
    db_session.add_all([user, product])
    await db_session.commit()

    response = await client.post(
        "/api/orders/",
        json={
            "items": [{"product_id": product.id, "quantity": 2}],
            "shipping_address_id": None,
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["total_amount"] == 59.98
    assert data["status"] == "pending"
    assert len(data["items"]) == 1


@pytest.mark.asyncio
async def test_create_order_insufficient_stock(client: AsyncClient, db_session):
    user = User(email="test2@example.com", username="testuser2", hashed_password="hash")
    product = Product(name="Rare Widget", sku="RWG-001", price=99.99, stock_quantity=1)
    db_session.add_all([user, product])
    await db_session.commit()

    response = await client.post(
        "/api/orders/",
        json={
            "items": [{"product_id": product.id, "quantity": 5}],
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 400
    assert "Insufficient stock" in response.json()["detail"]


@pytest.mark.asyncio
async def test_cancel_order_restores_stock(client: AsyncClient, db_session):
    user = User(email="test3@example.com", username="testuser3", hashed_password="hash")
    product = Product(name="Gadget", sku="GDG-001", price=49.99, stock_quantity=50)
    db_session.add_all([user, product])
    await db_session.commit()

    # Create order
    create_resp = await client.post(
        "/api/orders/",
        json={"items": [{"product_id": product.id, "quantity": 10}]},
        headers={"Authorization": "Bearer test-token"},
    )
    order_id = create_resp.json()["id"]

    # Cancel
    cancel_resp = await client.delete(
        f"/api/orders/{order_id}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert cancel_resp.status_code == 204

    # Verify stock restored
    await db_session.refresh(product)
    assert product.stock_quantity == 50


@pytest.mark.asyncio
async def test_order_status_transitions(client: AsyncClient, db_session):
    user = User(email="test4@example.com", username="testuser4", hashed_password="hash")
    product = Product(name="Item", sku="ITM-001", price=19.99, stock_quantity=10)
    db_session.add_all([user, product])
    await db_session.commit()

    # Create
    resp = await client.post(
        "/api/orders/",
        json={"items": [{"product_id": product.id, "quantity": 1}]},
        headers={"Authorization": "Bearer test-token"},
    )
    order_id = resp.json()["id"]

    # pending -> confirmed
    resp = await client.patch(
        f"/api/orders/{order_id}",
        json={"status": "confirmed"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"

    # confirmed -> delivered (invalid)
    resp = await client.patch(
        f"/api/orders/{order_id}",
        json={"status": "delivered"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 400
""")


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _number_lines(content: str) -> str:
    """Add line numbers like the Read tool output."""
    return "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(content.split("\n")))


def _tool_call(name: str, args: dict[str, Any]) -> dict:
    return {
        "id": _next_call_id(),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


def _assistant_with_tools(content: str | None, *tool_calls: dict) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = list(tool_calls)
    return msg


def _tool_result(tool_call_id: str, content: str, name: str = "Read") -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
        "name": name,
    }


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def build_coding_session_short() -> list[dict]:
    """A short 8-turn coding session (below cold start threshold).

    Exercises basic proxy passthrough — not enough turns for assembly.
    ~8K tokens total.
    """
    _reset_counter()
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Turn 1: user asks to add a feature
    messages.append({"role": "user", "content": "I need to add a discount field to the Order model. The discount should be a percentage (0-100) applied to the total_amount."})

    # Turn 2: assistant reads models.py
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/models.py"})
    messages.append(_assistant_with_tools("Let me read the current Order model first.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(_MODELS_PY)))

    # Turn 3: user approves
    messages.append({"role": "user", "content": "Yes, add it after total_amount."})

    # Turn 4: assistant edits
    tc = _tool_call("Edit", {
        "file_path": "/workspace/myapp/app/models.py",
        "old_string": "    total_amount: Mapped[float] = mapped_column(Float, nullable=False)",
        "new_string": (
            "    total_amount: Mapped[float] = mapped_column(Float, nullable=False)\n"
            "    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100"
        ),
    })
    messages.append(_assistant_with_tools("I'll add the discount_pct field.", tc))
    messages.append(_tool_result(tc["id"], "Edit applied successfully.", "Edit"))

    return messages


def build_coding_session_long() -> list[dict]:
    """A long 25+ turn coding session that exceeds 50K tokens.

    Exercises context assembly, message rewriting, and token savings.
    Simulates implementing a new feature: adding a review system to
    the e-commerce app with multiple file reads, edits, test runs.
    """
    _reset_counter()
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # --- Phase 1: Explore the codebase (turns 1-6) ---

    messages.append({"role": "user", "content": "I want to add product reviews to the app. Users should be able to rate products 1-5 stars and leave a text review. Show me what we have so far."})

    # Read models
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/models.py"})
    messages.append(_assistant_with_tools("Let me explore the codebase. I'll start with the models.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(_MODELS_PY)))

    # Read routes
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/routes/orders.py"})
    messages.append(_assistant_with_tools(
        "Good — I see there's already a Review model defined but no routes for it yet. "
        "Let me check the existing route patterns.",
        tc,
    ))
    messages.append(_tool_result(tc["id"], _number_lines(_ROUTES_PY)))

    # Search for existing review code
    tc = _tool_call("Grep", {"pattern": "review", "path": "/workspace/myapp/", "glob": "*.py"})
    messages.append(_assistant_with_tools(
        "Let me search for any existing review-related code.",
        tc,
    ))
    messages.append(_tool_result(tc["id"], textwrap.dedent("""\
        /workspace/myapp/app/models.py:136:class Review(Base):
        /workspace/myapp/app/models.py:137:    __tablename__ = "reviews"
        /workspace/myapp/app/models.py:33:    reviews: Mapped[list["Review"]] = relationship("Review", back_populates="user")
        /workspace/myapp/app/models.py:62:    reviews: Mapped[list["Review"]] = relationship("Review", back_populates="product")
    """), "Grep"))

    messages.append({"role": "user", "content": "Good, the Review model exists. Now create the review routes. I want: POST /api/reviews (create), GET /api/reviews/{review_id}, GET /api/products/{product_id}/reviews (list), and DELETE /api/reviews/{review_id}."})

    # Read conftest for test patterns
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/tests/conftest.py"})
    messages.append(_assistant_with_tools(
        "Let me check the test setup to match the existing patterns before writing the routes.",
        tc,
    ))
    messages.append(_tool_result(tc["id"], _number_lines(_CONFTEST_PY)))

    # --- Phase 2: Implement (turns 7-14) ---

    # Write the review routes
    review_routes_content = textwrap.dedent("""\
    \"\"\"Review API endpoints.\"\"\"

    from fastapi import APIRouter, Depends, HTTPException, Query
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import select, func

    from app.database import get_db
    from app.models import Review, Product, User
    from app.schemas import ReviewCreate, ReviewResponse, ReviewListResponse
    from app.auth import get_current_user

    router = APIRouter(prefix="/api/reviews", tags=["reviews"])


    @router.post("/", response_model=ReviewResponse, status_code=201)
    async def create_review(
        review_data: ReviewCreate,
        db: AsyncSession = Depends(get_db),
        user: User = Depends(get_current_user),
    ):
        # Check product exists
        product = await db.get(Product, review_data.product_id)
        if not product:
            raise HTTPException(404, "Product not found")

        # Check user hasn't already reviewed this product
        existing = await db.execute(
            select(Review).where(
                Review.user_id == user.id,
                Review.product_id == review_data.product_id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(400, "You have already reviewed this product")

        review = Review(
            user_id=user.id,
            product_id=review_data.product_id,
            rating=review_data.rating,
            title=review_data.title,
            body=review_data.body,
        )
        db.add(review)
        await db.commit()
        await db.refresh(review)
        return ReviewResponse.model_validate(review)


    @router.get("/{review_id}", response_model=ReviewResponse)
    async def get_review(
        review_id: int,
        db: AsyncSession = Depends(get_db),
    ):
        review = await db.get(Review, review_id)
        if not review:
            raise HTTPException(404, "Review not found")
        return ReviewResponse.model_validate(review)


    @router.delete("/{review_id}", status_code=204)
    async def delete_review(
        review_id: int,
        db: AsyncSession = Depends(get_db),
        user: User = Depends(get_current_user),
    ):
        review = await db.get(Review, review_id)
        if not review:
            raise HTTPException(404, "Review not found")
        if review.user_id != user.id and not user.is_superuser:
            raise HTTPException(403, "Not authorized to delete this review")
        await db.delete(review)
        await db.commit()
    """)

    tc = _tool_call("Write", {
        "file_path": "/workspace/myapp/app/routes/reviews.py",
        "content": review_routes_content,
    })
    messages.append(_assistant_with_tools(
        "I'll create the review routes following the same patterns as the orders API. "
        "Here's the implementation with all four endpoints:",
        tc,
    ))
    messages.append(_tool_result(tc["id"], "File written successfully.", "Write"))

    # Register routes in main.py
    messages.append({"role": "user", "content": "Good. Don't forget to register the router in main.py."})

    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/main.py"})
    messages.append(_assistant_with_tools("Let me check the current main.py.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(textwrap.dedent("""\
    from fastapi import FastAPI
    from app.routes.orders import router as orders_router
    from app.database import init_db

    def create_app() -> FastAPI:
        app = FastAPI(title="MyApp", version="0.1.0")
        app.include_router(orders_router)

        @app.on_event("startup")
        async def startup():
            await init_db()

        return app
    """))))

    tc = _tool_call("Edit", {
        "file_path": "/workspace/myapp/app/main.py",
        "old_string": "from app.routes.orders import router as orders_router",
        "new_string": "from app.routes.orders import router as orders_router\nfrom app.routes.reviews import router as reviews_router",
    })
    messages.append(_assistant_with_tools("I'll add the reviews router import and registration.", tc))
    messages.append(_tool_result(tc["id"], "Edit applied successfully.", "Edit"))

    tc = _tool_call("Edit", {
        "file_path": "/workspace/myapp/app/main.py",
        "old_string": "    app.include_router(orders_router)",
        "new_string": "    app.include_router(orders_router)\n    app.include_router(reviews_router)",
    })
    messages.append(_assistant_with_tools(None, tc))
    messages.append(_tool_result(tc["id"], "Edit applied successfully.", "Edit"))

    # Write schemas
    messages.append({"role": "user", "content": "We also need the Pydantic schemas for ReviewCreate, ReviewResponse, ReviewListResponse."})

    schemas_content = textwrap.dedent("""\
    from pydantic import BaseModel, Field
    from typing import Optional
    from datetime import datetime


    class ReviewCreate(BaseModel):
        product_id: int
        rating: int = Field(ge=1, le=5)
        title: Optional[str] = Field(None, max_length=200)
        body: Optional[str] = None


    class ReviewResponse(BaseModel):
        id: int
        user_id: int
        product_id: int
        rating: int
        title: Optional[str]
        body: Optional[str]
        created_at: datetime

        model_config = {"from_attributes": True}


    class ReviewListResponse(BaseModel):
        reviews: list[ReviewResponse]
        total: int
        average_rating: Optional[float]
    """)

    tc = _tool_call("Write", {
        "file_path": "/workspace/myapp/app/schemas/reviews.py",
        "content": schemas_content,
    })
    messages.append(_assistant_with_tools("Here are the Pydantic schemas:", tc))
    messages.append(_tool_result(tc["id"], "File written successfully.", "Write"))

    # --- Phase 3: Tests (turns 15-20) ---

    # Read existing test patterns
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/tests/test_orders.py"})
    messages.append(_assistant_with_tools(
        "Now let me write tests. I'll check the existing test patterns first.",
        tc,
    ))
    messages.append(_tool_result(tc["id"], _number_lines(_TEST_ORDERS_PY)))

    messages.append({"role": "user", "content": "Write tests for the review endpoints. Cover: create success, duplicate review rejection, delete by owner, delete by non-owner, get by id, list by product."})

    review_tests = textwrap.dedent("""\
    import pytest
    from httpx import AsyncClient
    from app.models import User, Product, Review


    @pytest.mark.asyncio
    async def test_create_review(client: AsyncClient, db_session):
        user = User(email="rev1@example.com", username="reviewer1", hashed_password="hash")
        product = Product(name="TestProd", sku="TST-001", price=9.99, stock_quantity=10)
        db_session.add_all([user, product])
        await db_session.commit()

        resp = await client.post(
            "/api/reviews/",
            json={"product_id": product.id, "rating": 5, "title": "Great!", "body": "Love it."},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 201
        assert resp.json()["rating"] == 5


    @pytest.mark.asyncio
    async def test_duplicate_review_rejected(client: AsyncClient, db_session):
        user = User(email="rev2@example.com", username="reviewer2", hashed_password="hash")
        product = Product(name="TestProd2", sku="TST-002", price=9.99, stock_quantity=10)
        db_session.add_all([user, product])
        await db_session.commit()

        # First review
        await client.post(
            "/api/reviews/",
            json={"product_id": product.id, "rating": 4},
            headers={"Authorization": "Bearer test-token"},
        )

        # Duplicate
        resp = await client.post(
            "/api/reviews/",
            json={"product_id": product.id, "rating": 3},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 400
        assert "already reviewed" in resp.json()["detail"]


    @pytest.mark.asyncio
    async def test_delete_review_owner(client: AsyncClient, db_session):
        user = User(email="rev3@example.com", username="reviewer3", hashed_password="hash")
        product = Product(name="TestProd3", sku="TST-003", price=9.99, stock_quantity=10)
        db_session.add_all([user, product])
        await db_session.commit()

        create_resp = await client.post(
            "/api/reviews/",
            json={"product_id": product.id, "rating": 2, "body": "Not great."},
            headers={"Authorization": "Bearer test-token"},
        )
        review_id = create_resp.json()["id"]

        del_resp = await client.delete(
            f"/api/reviews/{review_id}",
            headers={"Authorization": "Bearer test-token"},
        )
        assert del_resp.status_code == 204


    @pytest.mark.asyncio
    async def test_get_review(client: AsyncClient, db_session):
        user = User(email="rev4@example.com", username="reviewer4", hashed_password="hash")
        product = Product(name="TestProd4", sku="TST-004", price=19.99, stock_quantity=5)
        review = Review(user_id=1, product_id=1, rating=4, title="Good", body="Solid product.")
        db_session.add_all([user, product])
        await db_session.commit()
        review.user_id = user.id
        review.product_id = product.id
        db_session.add(review)
        await db_session.commit()

        resp = await client.get(f"/api/reviews/{review.id}")
        assert resp.status_code == 200
        assert resp.json()["rating"] == 4
    """)

    tc = _tool_call("Write", {
        "file_path": "/workspace/myapp/tests/test_reviews.py",
        "content": review_tests,
    })
    messages.append(_assistant_with_tools("Here are the tests for the review endpoints:", tc))
    messages.append(_tool_result(tc["id"], "File written successfully.", "Write"))

    # Run tests
    tc = _tool_call("Bash", {"command": "cd /workspace/myapp && python -m pytest tests/test_reviews.py -v"})
    messages.append(_assistant_with_tools("Let me run the tests.", tc))
    messages.append(_tool_result(tc["id"], textwrap.dedent("""\
        ============================= test session starts ==============================
        platform linux -- Python 3.12.3, pytest-8.3.3
        collected 4 items

        tests/test_reviews.py::test_create_review PASSED                        [ 25%]
        tests/test_reviews.py::test_duplicate_review_rejected PASSED            [ 50%]
        tests/test_reviews.py::test_delete_review_owner PASSED                  [ 75%]
        tests/test_reviews.py::test_get_review PASSED                           [100%]

        ============================== 4 passed in 2.34s ===============================
    """), "Bash"))

    messages.append({"role": "user", "content": "Tests pass. Now add the product listing endpoint that shows average rating."})

    # Read routes again to modify
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/routes/reviews.py"})
    messages.append(_assistant_with_tools("Let me re-read the reviews routes to add the product listing endpoint.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(review_routes_content)))

    product_reviews_endpoint = textwrap.dedent("""\

    @router.get("/products/{product_id}/reviews", response_model=ReviewListResponse)
    async def list_product_reviews(
        product_id: int,
        db: AsyncSession = Depends(get_db),
        page: int = Query(1, ge=1),
        per_page: int = Query(20, ge=1, le=100),
        sort_by: str = Query("newest", pattern="^(newest|oldest|highest|lowest)$"),
    ):
        product = await db.get(Product, product_id)
        if not product:
            raise HTTPException(404, "Product not found")

        query = select(Review).where(Review.product_id == product_id)

        sort_map = {
            "newest": Review.created_at.desc(),
            "oldest": Review.created_at.asc(),
            "highest": Review.rating.desc(),
            "lowest": Review.rating.asc(),
        }
        query = query.order_by(sort_map[sort_by])

        count_q = select(func.count()).select_from(query.subquery())
        total = (await db.execute(count_q)).scalar()

        avg_q = select(func.avg(Review.rating)).where(Review.product_id == product_id)
        avg_rating = (await db.execute(avg_q)).scalar()

        query = query.offset((page - 1) * per_page).limit(per_page)
        result = await db.execute(query)
        reviews = result.scalars().all()

        return ReviewListResponse(
            reviews=[ReviewResponse.model_validate(r) for r in reviews],
            total=total,
            average_rating=round(float(avg_rating), 2) if avg_rating else None,
        )
    """)

    tc = _tool_call("Edit", {
        "file_path": "/workspace/myapp/app/routes/reviews.py",
        "old_string": "        await db.commit()\n",
        "new_string": "        await db.commit()\n" + product_reviews_endpoint,
    })
    messages.append(_assistant_with_tools(
        "I'll add the product reviews listing endpoint with sorting and pagination:",
        tc,
    ))
    messages.append(_tool_result(tc["id"], "Edit applied successfully.", "Edit"))

    # --- Phase 4: Bug fix and polish (turns 21-25) ---

    messages.append({"role": "user", "content": "I notice the review routes don't have proper input validation. The rating should be constrained to 1-5 in the route handler too, not just the schema. And add a 2000 character limit on the body."})

    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/routes/reviews.py"})
    messages.append(_assistant_with_tools("Let me re-read the current routes to add validation.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(review_routes_content + product_reviews_endpoint)))

    tc = _tool_call("Edit", {
        "file_path": "/workspace/myapp/app/routes/reviews.py",
        "old_string": "        review = Review(",
        "new_string": (
            "        # Server-side validation\n"
            "        if not (1 <= review_data.rating <= 5):\n"
            "            raise HTTPException(400, 'Rating must be between 1 and 5')\n"
            "        if review_data.body and len(review_data.body) > 2000:\n"
            "            raise HTTPException(400, 'Review body must be under 2000 characters')\n\n"
            "        review = Review("
        ),
    })
    messages.append(_assistant_with_tools("Adding server-side validation:", tc))
    messages.append(_tool_result(tc["id"], "Edit applied successfully.", "Edit"))

    # Run full test suite
    tc = _tool_call("Bash", {"command": "cd /workspace/myapp && python -m pytest tests/ -v --tb=short"})
    messages.append(_assistant_with_tools("Let me run the full test suite to make sure nothing is broken.", tc))
    messages.append(_tool_result(tc["id"], textwrap.dedent("""\
        ============================= test session starts ==============================
        platform linux -- Python 3.12.3, pytest-8.3.3
        collected 8 items

        tests/test_orders.py::test_create_order_success PASSED                  [ 12%]
        tests/test_orders.py::test_create_order_insufficient_stock PASSED       [ 25%]
        tests/test_orders.py::test_cancel_order_restores_stock PASSED           [ 37%]
        tests/test_orders.py::test_order_status_transitions PASSED              [ 50%]
        tests/test_reviews.py::test_create_review PASSED                        [ 62%]
        tests/test_reviews.py::test_duplicate_review_rejected PASSED            [ 75%]
        tests/test_reviews.py::test_delete_review_owner PASSED                  [ 87%]
        tests/test_reviews.py::test_get_review PASSED                           [100%]

        ============================== 8 passed in 3.67s ===============================
    """), "Bash"))

    # Git commit
    messages.append({"role": "user", "content": "Looks good! Commit everything."})

    tc = _tool_call("Bash", {"command": "cd /workspace/myapp && git add app/routes/reviews.py app/schemas/reviews.py app/main.py tests/test_reviews.py && git commit -m 'feat: add product review endpoints with tests'"})
    messages.append(_assistant_with_tools("Committing all changes:", tc))
    messages.append(_tool_result(tc["id"], textwrap.dedent("""\
        [main abc1234] feat: add product review endpoints with tests
         4 files changed, 187 insertions(+), 1 deletion(-)
         create mode 100644 app/routes/reviews.py
         create mode 100644 app/schemas/reviews.py
         create mode 100644 tests/test_reviews.py
    """), "Bash"))

    # Alembic migration
    messages.append({"role": "user", "content": "We need an Alembic migration too. The Review table might not exist yet in production."})

    tc = _tool_call("Bash", {"command": "cd /workspace/myapp && alembic revision --autogenerate -m 'add reviews table'"})
    messages.append(_assistant_with_tools("Generating the migration:", tc))
    messages.append(_tool_result(tc["id"], textwrap.dedent("""\
        INFO  [alembic.runtime.migration] Context impl SQLiteImpl.
        INFO  [alembic.runtime.migration] Will assume non-transactional DDL.
        INFO  [alembic.autogenerate.compare] Detected added table 'reviews'
        Generating /workspace/myapp/alembic/versions/abc123_add_reviews_table.py ...  done
    """), "Bash"))

    return messages


def build_recall_trigger_session() -> list[dict]:
    """A session designed to trigger __archolith_recall.

    Establishes facts in early turns (file structure, database schema,
    API contracts), then asks questions in later turns that require
    recall from the session graph.

    The conversation pattern:
    1-5: Establish context (read files, understand architecture)
    6-10: Implement changes (write code, run tests)
    11-15: Ask questions that reference earlier context (triggers recall)
    """
    _reset_counter()
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # --- Establish facts (turns 1-5) ---

    messages.append({"role": "user", "content": (
        "I'm working on a microservices project. The auth service uses JWT tokens "
        "with RS256 signing. Keys rotate every 30 days. The JWKS endpoint is at "
        "https://auth.internal/api/v2/.well-known/jwks.json. "
        "The order service validates tokens by fetching the JWKS and caching for 5 minutes. "
        "Can you read the auth middleware?"
    )})

    auth_middleware = textwrap.dedent("""\
    \"\"\"JWT authentication middleware for the order service.\"\"\"

    import time
    from functools import lru_cache
    from typing import Any

    import httpx
    import jwt
    from fastapi import Depends, HTTPException, Security
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    JWKS_URL = "https://auth.internal/api/v2/.well-known/jwks.json"
    JWKS_CACHE_TTL = 300  # 5 minutes
    ISSUER = "https://auth.internal"
    AUDIENCE = "order-service"

    _jwks_cache: dict[str, Any] = {}
    _jwks_fetched_at: float = 0


    async def _fetch_jwks() -> dict:
        global _jwks_cache, _jwks_fetched_at
        now = time.time()
        if _jwks_cache and (now - _jwks_fetched_at) < JWKS_CACHE_TTL:
            return _jwks_cache
        async with httpx.AsyncClient() as client:
            resp = await client.get(JWKS_URL, timeout=10.0)
            resp.raise_for_status()
            _jwks_cache = resp.json()
            _jwks_fetched_at = now
            return _jwks_cache


    async def validate_token(
        credentials: HTTPAuthorizationCredentials = Security(HTTPBearer()),
    ) -> dict:
        token = credentials.credentials
        try:
            jwks = await _fetch_jwks()
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if not kid:
                raise HTTPException(401, "Token missing key ID")
            key = None
            for jwk in jwks.get("keys", []):
                if jwk.get("kid") == kid:
                    key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
                    break
            if not key:
                raise HTTPException(401, f"Unknown key ID: {kid}")
            payload = jwt.decode(
                token, key, algorithms=["RS256"],
                issuer=ISSUER, audience=AUDIENCE,
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(401, "Token expired")
        except jwt.InvalidTokenError as e:
            raise HTTPException(401, f"Invalid token: {e}")
    """)

    tc = _tool_call("Read", {"file_path": "/workspace/order-service/app/auth/middleware.py"})
    messages.append(_assistant_with_tools("Let me read the auth middleware.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(auth_middleware)))

    # Establish database config
    messages.append({"role": "user", "content": (
        "The order service database is PostgreSQL at postgres://orders:s3cr3t@db-orders.internal:5432/orders_prod. "
        "Connection pool: min 5, max 20, overflow 10. We use SQLAlchemy 2.0 with asyncpg driver. "
        "The read replica is at db-orders-replica.internal:5432. "
        "Read the database config."
    )})

    db_config = textwrap.dedent("""\
    \"\"\"Database configuration and session management.\"\"\"

    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    PRIMARY_URL = "postgresql+asyncpg://orders:s3cr3t@db-orders.internal:5432/orders_prod"
    REPLICA_URL = "postgresql+asyncpg://orders:s3cr3t@db-orders-replica.internal:5432/orders_prod"

    primary_engine = create_async_engine(
        PRIMARY_URL,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
    )

    replica_engine = create_async_engine(
        REPLICA_URL,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )

    PrimarySession = async_sessionmaker(primary_engine, class_=AsyncSession, expire_on_commit=False)
    ReplicaSession = async_sessionmaker(replica_engine, class_=AsyncSession, expire_on_commit=False)
    """)

    tc = _tool_call("Read", {"file_path": "/workspace/order-service/app/database.py"})
    messages.append(_assistant_with_tools("Reading the database config.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(db_config)))

    # Establish deployment details
    messages.append({"role": "user", "content": (
        "We deploy on Kubernetes. The order service runs 3 replicas behind a ClusterIP service "
        "on port 8080. Health checks: /health/live (liveness, 10s interval) and "
        "/health/ready (readiness, checks DB connection + JWKS cache). "
        "Environment: ENVIRONMENT=production, LOG_LEVEL=info, SENTRY_DSN=https://abc@sentry.internal/42. "
        "Secrets are mounted from k8s Secret 'order-service-secrets' at /etc/secrets/. "
        "Now read the deployment manifest."
    )})

    k8s_manifest = textwrap.dedent("""\
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: order-service
      namespace: production
      labels:
        app: order-service
        version: v2.4.1
    spec:
      replicas: 3
      selector:
        matchLabels:
          app: order-service
      template:
        metadata:
          labels:
            app: order-service
            version: v2.4.1
        spec:
          containers:
          - name: order-service
            image: registry.internal/order-service:v2.4.1
            ports:
            - containerPort: 8080
            env:
            - name: ENVIRONMENT
              value: "production"
            - name: LOG_LEVEL
              value: "info"
            - name: SENTRY_DSN
              valueFrom:
                secretKeyRef:
                  name: order-service-secrets
                  key: sentry-dsn
            livenessProbe:
              httpGet:
                path: /health/live
                port: 8080
              initialDelaySeconds: 10
              periodSeconds: 10
            readinessProbe:
              httpGet:
                path: /health/ready
                port: 8080
              initialDelaySeconds: 5
              periodSeconds: 10
            resources:
              requests:
                cpu: "200m"
                memory: "256Mi"
              limits:
                cpu: "500m"
                memory: "512Mi"
            volumeMounts:
            - name: secrets
              mountPath: /etc/secrets
              readOnly: true
          volumes:
          - name: secrets
            secret:
              secretName: order-service-secrets
    """)

    tc = _tool_call("Read", {"file_path": "/workspace/order-service/k8s/deployment.yaml"})
    messages.append(_assistant_with_tools("Reading the k8s deployment manifest.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(k8s_manifest)))

    # --- Middle turns: implementation work (turns 6-10) ---

    messages.append({"role": "user", "content": "I need to add rate limiting to the order creation endpoint. Use a Redis-backed sliding window counter. Max 10 orders per user per minute."})

    messages.append(_assistant_with_tools(
        "I'll implement a Redis-backed sliding window rate limiter. Based on what I saw earlier, "
        "the order service uses asyncpg for PostgreSQL. For Redis, I'll use redis-py with async support.\n\n"
        "The rate limiter will:\n"
        "1. Use a sorted set per user with timestamps as scores\n"
        "2. Remove entries older than the window (60s)\n"
        "3. Count remaining entries\n"
        "4. Add the new timestamp if under limit\n"
        "5. Return 429 Too Many Requests if over limit\n\n"
        "Let me write the implementation.",
        _tool_call("Write", {
            "file_path": "/workspace/order-service/app/ratelimit.py",
            "content": "# Rate limiter implementation...\n" * 30,
        }),
    ))
    messages.append(_tool_result(messages[-1]["tool_calls"][0]["id"], "File written successfully.", "Write"))

    # More implementation work
    for i in range(4):
        messages.append({"role": "user", "content": f"Continue with the rate limiter integration (step {i + 2})."})
        tc = _tool_call("Bash", {"command": f"echo 'Step {i + 2} completed'"})
        messages.append(_assistant_with_tools(f"Proceeding with step {i + 2} of the integration.", tc))
        messages.append(_tool_result(tc["id"], f"Step {i + 2} completed\n", "Bash"))

    # --- Late turns: questions requiring recall (turns 11-15) ---

    messages.append({"role": "user", "content": (
        "I'm now debugging a production issue. The order service is returning 401 errors intermittently. "
        "What was the JWKS cache TTL we set in the auth middleware? And what's the JWKS endpoint URL?"
    )})

    messages.append({"role": "assistant", "content": (
        "Based on the auth middleware I read earlier, the JWKS cache TTL is 300 seconds (5 minutes) "
        "and the JWKS endpoint is at https://auth.internal/api/v2/.well-known/jwks.json.\n\n"
        "The 401 errors could be caused by key rotation combined with the cache TTL — if the auth "
        "service rotates keys and the order service has stale JWKS cached, tokens signed with the "
        "new key would fail validation for up to 5 minutes."
    )})

    messages.append({"role": "user", "content": (
        "What database connection pool settings did we configure? I want to check if we're hitting pool exhaustion."
    )})

    messages.append({"role": "assistant", "content": (
        "The connection pool settings from the database config:\n"
        "- pool_size: 5 (minimum connections)\n"
        "- max_overflow: 10 (allows up to 15 total connections)\n"
        "- pool_pre_ping: True (validates connections before use)\n"
        "- pool_recycle: 1800 (recycle connections after 30 minutes)\n\n"
        "With 3 replicas, that's up to 45 total connections to the primary database. "
        "The read replica has the same pool settings, so another 45 to the replica."
    )})

    messages.append({"role": "user", "content": (
        "What Kubernetes resources are allocated to each pod? And how many replicas?"
    )})

    messages.append({"role": "assistant", "content": (
        "From the deployment manifest:\n"
        "- Replicas: 3\n"
        "- CPU: 200m request, 500m limit\n"
        "- Memory: 256Mi request, 512Mi limit\n"
        "- Health checks: /health/live (liveness, 10s), /health/ready (readiness, 10s)\n"
        "- Secrets mounted from 'order-service-secrets' at /etc/secrets/\n"
        "- Image: registry.internal/order-service:v2.4.1"
    )})

    return messages


def build_read_cache_session() -> list[dict]:
    """A session designed to test native Read interception via file cache.

    Reads the same files multiple times across turns. On the second read,
    the proxy should serve from cache (if native_read_intercept is enabled).
    """
    _reset_counter()
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Turn 1: Read models.py
    messages.append({"role": "user", "content": "Read models.py to understand the data model."})
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/models.py"})
    messages.append(_assistant_with_tools("Reading the models.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(_MODELS_PY)))

    # Turn 2: Read routes
    messages.append({"role": "user", "content": "Now read the order routes."})
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/routes/orders.py"})
    messages.append(_assistant_with_tools("Reading the order routes.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(_ROUTES_PY)))

    # Turn 3: Some implementation work
    messages.append({"role": "user", "content": "Add a discount_pct field to the Order model."})
    tc_edit = _tool_call("Edit", {
        "file_path": "/workspace/myapp/app/models.py",
        "old_string": "    total_amount:",
        "new_string": "    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)\n    total_amount:",
    })
    messages.append(_assistant_with_tools("Adding the field.", tc_edit))
    messages.append(_tool_result(tc_edit["id"], "Edit applied.", "Edit"))

    # Turn 4: Re-read models.py (should hit cache on second read)
    messages.append({"role": "user", "content": "Read models.py again to verify the change."})
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/models.py"})
    messages.append(_assistant_with_tools("Re-reading to verify.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(_MODELS_PY)))

    # Turn 5: Read conftest (new file)
    messages.append({"role": "user", "content": "Check the test setup."})
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/tests/conftest.py"})
    messages.append(_assistant_with_tools("Reading test configuration.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(_CONFTEST_PY)))

    # Turn 6: Re-read routes (should hit cache)
    messages.append({"role": "user", "content": "Check the order routes again — I want to add the discount to create_order."})
    tc = _tool_call("Read", {"file_path": "/workspace/myapp/app/routes/orders.py"})
    messages.append(_assistant_with_tools("Re-reading order routes.", tc))
    messages.append(_tool_result(tc["id"], _number_lines(_ROUTES_PY)))

    # Turn 7: Multiple reads in one turn
    messages.append({"role": "user", "content": "Read both models.py and conftest.py again for reference."})
    tc1 = _tool_call("Read", {"file_path": "/workspace/myapp/app/models.py"})
    tc2 = _tool_call("Read", {"file_path": "/workspace/myapp/tests/conftest.py"})
    messages.append(_assistant_with_tools("Reading both files.", tc1, tc2))
    messages.append(_tool_result(tc1["id"], _number_lines(_MODELS_PY)))
    messages.append(_tool_result(tc2["id"], _number_lines(_CONFTEST_PY)))

    return messages


def estimate_token_count(messages: list[dict]) -> int:
    """Rough token estimate for a conversation (4 chars ≈ 1 token)."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    total_chars += len(str(item.get("text", "")))
        # Tool calls
        for tc in (msg.get("tool_calls") or []):
            total_chars += len(json.dumps(tc))
    return total_chars // 4
