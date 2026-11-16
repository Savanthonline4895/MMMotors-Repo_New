"""API routes: auth (split out of server.py; code moved verbatim)."""
import secrets
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from database import JWT_EXPIRE_MIN, MAX_LOGIN_ATTEMPTS, LOGIN_LOCKOUT_MIN, pwd_ctx, create_token, verify_token, require_admin, oid, oids, obj_id, paginate_params, utcnow
from models import LoginIn, PasswordChange, TokenOut, UserCreate, UserUpdate
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════════════════

def _client_ip(request: Request) -> str:
    """Real visitor IP behind Render, which sits behind Cloudflare.

    Render's X-Forwarded-For is "<anything the client sent>, <client>, <Cloudflare edge>":
    the last entry is a Cloudflare edge shared by everyone nearby (so lockout and booking
    limits hit all local users together) and the first is client-controlled. Cloudflare
    overwrites CF-Connecting-IP with the real client, so prefer it; otherwise take the
    entry before the edge. Check on the live site with GET /api/auth/client-ip (owner)."""
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    if len(hops) >= 2:
        return hops[-2]
    if hops:
        return hops[0]
    return request.client.host if request.client else "unknown"


@api_router.get("/auth/client-ip")
async def client_ip_check(request: Request, current_user=Depends(require_admin)):
    """Owner-only: what the login lockout sees as your IP. It should match your real
    public IP (search "what is my ip"); a Cloudflare address means _client_ip needs work."""
    return {"client_ip": _client_ip(request),
            "x_forwarded_for": request.headers.get("x-forwarded-for", ""),
            "cf_connecting_ip": request.headers.get("cf-connecting-ip", "")}


_DUMMY_PW_HASH = pwd_ctx.hash(secrets.token_urlsafe(16))  # random; matches no password


@api_router.post("/auth/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request):
    username = body.username.strip().lower()
    ip       = _client_ip(request)
    # Per-(username, IP) lockout — attacker cannot lock real user from elsewhere
    attempt_count = await db.login_attempts.count_documents({"username": username, "ip": ip})
    if attempt_count >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts from this location. Try again in {LOGIN_LOCKOUT_MIN} minutes."
        )
    user = await db.users.find_one({"username": username})
    # Always run bcrypt, even for unknown usernames, so response time doesn't reveal which usernames exist
    pw_ok = pwd_ctx.verify(body.password, (user or {}).get("password") or _DUMMY_PW_HASH)
    if not user or not pw_ok:
        await db.login_attempts.insert_one({
            "username":   username,
            "ip":         ip,
            "created_at": utcnow(),
        })
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if user.get("status") != "active":
        raise HTTPException(status_code=403, detail="Account is inactive or deactivated")
    # Clear only this IP's failures for this user — preserve others as evidence
    await db.login_attempts.delete_many({"username": username, "ip": ip})
    token = create_token({"sub": str(user["_id"]), "role": user["role"]})
    user_data = {
        "id":       str(user["_id"]),
        "username": user["username"],
        "name":     user["name"],
        "role":     user["role"],
        "mobile":   user.get("mobile", ""),
        "allowed_pages": user.get("allowed_pages", None),
    }
    response = JSONResponse(content={"access_token": token, "token_type": "bearer", "user": user_data})
    response.set_cookie(
        key="mm_token", value=token, httponly=True, secure=True,
        samesite="none", max_age=60 * JWT_EXPIRE_MIN, path="/",
    )
    return response

@api_router.get("/auth/me")
async def me(current_user: dict = Depends(verify_token)):
    user = dict(current_user)
    user.pop("password", None)
    return user

@api_router.post("/auth/logout")
async def logout(current_user: dict = Depends(verify_token)):
    response = Response(content='{"message":"Logged out"}', media_type="application/json")
    response.delete_cookie(key="mm_token", path="/")
    return response


# ═══════════════════════════════════════════════════════════════════════════════
#  USERS / STAFF
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/users")
async def list_users(p=Depends(paginate_params), current_user=Depends(require_admin)):
    docs  = await db.users.find({}, {"password": 0}).skip(p["skip"]).limit(p["limit"]).to_list(p["limit"])
    total = await db.users.count_documents({})
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})

@api_router.post("/users", status_code=201)
async def create_user(body: UserCreate, current_user=Depends(require_admin)):
    existing = await db.users.find_one({"username": body.username.strip().lower()})
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")
    doc = body.dict()
    doc["username"] = doc["username"].strip().lower()
    temp_password = None
    if not doc.get("password"):
        temp_password = secrets.token_urlsafe(9)
        doc["password"] = temp_password
    doc["password"] = pwd_ctx.hash(doc["password"])
    doc["created_at"] = utcnow()
    result = await db.users.insert_one(doc)
    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None); doc.pop("password", None)
    if temp_password:
        doc["temp_password"] = temp_password  # returned once so the owner can hand it over
    return doc

@api_router.get("/users/{user_id}")
async def get_user(user_id: str, current_user=Depends(require_admin)):
    doc = await db.users.find_one({"_id": obj_id(user_id)}, {"password": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")
    return oid(doc)

@api_router.put("/users/{user_id}")
async def update_user(user_id: str, body: UserUpdate, current_user=Depends(require_admin)):
    update = {k: v for k, v in body.dict().items() if v is not None}
    # allowed_pages can be an empty list — treat None as "not provided", [] as "no pages"
    if body.allowed_pages is not None:
        update["allowed_pages"] = body.allowed_pages
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    await db.users.update_one({"_id": obj_id(user_id)}, {"$set": update})
    return oid(await db.users.find_one({"_id": obj_id(user_id)}, {"password": 0}))

@api_router.post("/users/{user_id}/password")
async def change_password(user_id: str, body: PasswordChange, current_user=Depends(require_admin)):
    await db.users.update_one({"_id": obj_id(user_id)}, {"$set": {"password": pwd_ctx.hash(body.new_password)}})
    return {"message": "Password updated"}

@api_router.delete("/users/{user_id}")
async def delete_user(user_id: str, current_user=Depends(require_admin)):
    if str(current_user["_id"]) == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    result = await db.users.delete_one({"_id": obj_id(user_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": "Deleted"}
