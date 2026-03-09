from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import requests
import os
import random 
import africastalking 
from datetime import datetime, timedelta

# --- SQLALCHEMY DATABASE IMPORTS ---
# Notice we added 'text' here for raw SQL execution
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, Boolean, text
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from sqlalchemy.exc import IntegrityError

# Initialize the Oletai Agri Finance Core API
app = FastAPI(title="Oletai Agri Finance Bank Core API", version="10.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# SMS ENGINE INITIALIZATION
# ==========================================
AT_USERNAME = os.getenv("AT_USERNAME", "sandbox")
AT_API_KEY = os.getenv("AT_API_KEY", "your_api_key")

africastalking.initialize(username=AT_USERNAME, api_key=AT_API_KEY)
sms = africastalking.SMS

# ==========================================
# DATABASE ARCHITECTURE
# ==========================================
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")
if not SQLALCHEMY_DATABASE_URL:
    raise ValueError("FATAL ERROR: DATABASE_URL environment variable is not set!")

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class FarmerDB(Base):
    __tablename__ = "farmers"
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True)
    user_segment = Column(String) 
    identifier = Column(String)    
    farm_size_acres = Column(Float, default=0.0)
    trust_score = Column(Float)
    approved_limit = Column(Integer)
    country_code = Column(String, default="KE")
    base_currency = Column(String, default="KES")
    created_at = Column(DateTime, default=datetime.utcnow)

class TransactionDB(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    mpesa_receipt = Column(String, unique=True, index=True)
    farmer_phone = Column(String)
    till_number = Column(String)
    agent_phone = Column(String, nullable=True) # --- ADDED: Tracks which agent made the sale
    amount_kes = Column(Integer)
    facility_fee = Column(Integer)
    timestamp = Column(DateTime, default=datetime.utcnow)

class OTPStoreDB(Base):
    __tablename__ = "otp_store"
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, index=True)
    otp_code = Column(String)
    expires_at = Column(DateTime)

class AgrovetDB(Base):
    __tablename__ = "agrovets"
    id = Column(Integer, primary_key=True, index=True)
    business_name = Column(String, index=True)
    till_number = Column(String, unique=True, index=True)
    owner_phone = Column(String)
    location = Column(String)
    is_active = Column(Boolean, default=True) 
    created_at = Column(DateTime, default=datetime.utcnow)

# --- NEW: AGENT DATABASE TABLE ---
class AgentDB(Base):
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String)
    phone_number = Column(String, unique=True, index=True)
    commission_balance = Column(Integer, default=0) 
    total_sales_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

# 1. Create all new tables (like 'agents')
Base.metadata.create_all(bind=engine)

# 2. SELF-HEALING MIGRATION: Force missing columns into existing tables
try:
    with engine.connect() as conn:
        # Fix the transactions table
        conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS agent_phone VARCHAR"))
        
        # Fix the agents table completely
        conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS commission_balance INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_sales_count INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
        
        conn.commit()
except Exception as e:
    print(f"Migration Notice: {e}")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# DATA MODELS
# ==========================================
class SendOTPRequest(BaseModel):
    phone_number: str

class VerifyOTPRequest(BaseModel):
    phone_number: str
    otp_code: str

class LoanRequest(BaseModel):
    phone_number: str
    user_segment: str = "Farmer" 
    identifier: str              
    farm_size_acres: float = 0.0
    repayment_history_multiplier: float = 1.0

class DisburseRequest(BaseModel):
    phone_number: str
    till_number: str
    amount_kes: int

class RepayRequest(BaseModel):
    phone_number: str
    amount: int

class AgrovetRegisterRequest(BaseModel):
    business_name: str
    till_number: str
    owner_phone: str
    location: str

class AgentRegisterRequest(BaseModel):
    agent_name: str
    phone_number: str

class AgentSaleRequest(BaseModel):
    agent_phone: str
    farmer_phone: str
    amount_kes: int
    product_name: str 

# ==========================================
# MULTI-SEGMENT SCORING ALGORITHM
# ==========================================
def calculate_multi_segment_score(segment: str, identifier: str, units: float):
    if segment == "Student":
        return {"tier": "Elimu Starter", "amount": 5000, "fee": 400, "score": 45.0}
    elif segment == "Professional":
        return {"tier": "Urban Maisha", "amount": 25000, "fee": 2000, "score": 65.0}
    else: 
        crop_scores = {"avocado": 10, "coffee": 8, "maize": 4}
        c_score = crop_scores.get(identifier.lower(), 3)
        f_score = 10 if units >= 5.0 else 7 if units >= 2.0 else 5
        base_score = (c_score * 0.6) + (f_score * 0.4)
        trust_score = (base_score * 10)
        
        if trust_score <= 40: return {"tier": "Tier 1: Seed", "amount": 2500, "fee": 200, "score": trust_score}
        elif trust_score <= 70: return {"tier": "Tier 2: Growth", "amount": 10000, "fee": 800, "score": trust_score}
        else: return {"tier": "Tier 3: Harvest", "amount": 50000, "fee": 4000, "score": trust_score}

# ==========================================
# CORE ENDPOINTS
# ==========================================

@app.post("/api/v1/auth/send-otp")
async def send_otp(request: SendOTPRequest, db: Session = Depends(get_db)):
    try:
        code = str(random.randint(1000, 9999))
        expiry = datetime.utcnow() + timedelta(minutes=10)
        new_otp = OTPStoreDB(phone_number=request.phone_number, otp_code=code, expires_at=expiry)
        db.add(new_otp)
        db.commit()
        sms.send(f"Welcome to Oletai Bank! Your PIN: {code}", [request.phone_number])
        return {"status": "success", "message": "OTP sent"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/auth/verify-otp")
async def verify_otp(request: VerifyOTPRequest, db: Session = Depends(get_db)):
    valid_otp = db.query(OTPStoreDB).filter(OTPStoreDB.phone_number == request.phone_number, OTPStoreDB.otp_code == request.otp_code, OTPStoreDB.expires_at > datetime.utcnow()).first()
    if not valid_otp:
        raise HTTPException(status_code=400, detail="Invalid PIN")
    user = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
    db.delete(valid_otp)
    db.commit()
    return {"status": "success", "is_new_user": user is None, "score": user.trust_score if user else 0, "limit": user.approved_limit if user else 0}

@app.post("/api/v1/apply-loan")
async def apply_for_loan(request: LoanRequest, db: Session = Depends(get_db)):
    try:
        decision = calculate_multi_segment_score(request.user_segment, request.identifier, request.farm_size_acres)
        user = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
        
        if not user:
            user = FarmerDB(
                phone_number=request.phone_number,
                user_segment=request.user_segment,
                identifier=request.identifier,
                farm_size_acres=request.farm_size_acres,
                trust_score=decision["score"],
                approved_limit=decision["amount"]
            )
            db.add(user)
        else:
            user.trust_score = decision["score"]
            user.approved_limit = decision["amount"]
        
        db.commit()
        return {"status": "success", "decision": decision}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/transactions/{phone_number}")
async def get_transaction_history(phone_number: str, db: Session = Depends(get_db)):
    history = db.query(TransactionDB).filter(TransactionDB.farmer_phone == phone_number).order_by(TransactionDB.timestamp.desc()).all()
    return [
        {
            "id": tx.id,
            "title": "Loan Repayment" if tx.till_number == "OLETAI_BANK" else f"Payment (Till {tx.till_number})",
            "date": tx.timestamp.strftime("%b %d, %I:%M %p"),
            "amount": f"{'+' if tx.till_number == 'OLETAI_BANK' else '-'} KES {tx.amount_kes}",
            "is_credit": tx.till_number == "OLETAI_BANK"
        } for tx in history
    ]

@app.post("/api/v1/disburse")
async def disburse_funds(request: DisburseRequest, db: Session = Depends(get_db)):
    receipt_code = f"OLETAI_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    new_tx = TransactionDB(mpesa_receipt=receipt_code, farmer_phone=request.phone_number, till_number=request.till_number, amount_kes=request.amount_kes, facility_fee=int(request.amount_kes * 0.08))
    db.add(new_tx)
    db.commit()
    return {"status": "success", "mpesa_receipt": receipt_code}

@app.post("/api/v1/repay")
async def repay_loan(request: RepayRequest, db: Session = Depends(get_db)):
    try:
        user = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        repayment_ref = f"REPAY_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        new_tx = TransactionDB(mpesa_receipt=repayment_ref, farmer_phone=request.phone_number, till_number="OLETAI_BANK", amount_kes=request.amount, facility_fee=0)
        db.add(new_tx)
        user.trust_score = min(user.trust_score + 5.0, 100.0)
        db.commit()
        return {"status": "success", "new_score": user.trust_score}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/agrovets/register")
async def register_agrovet(request: AgrovetRegisterRequest, db: Session = Depends(get_db)):
    try:
        new_agrovet = AgrovetDB(
            business_name=request.business_name,
            till_number=request.till_number,
            owner_phone=request.owner_phone,
            location=request.location
        )
        db.add(new_agrovet)
        db.commit()
        db.refresh(new_agrovet)
        return {"status": "success", "message": "Agrovet registered successfully"}
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="This Till Number is already registered.")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/agrovets")
async def get_active_agrovets(db: Session = Depends(get_db)):
    try:
        agrovets = db.query(AgrovetDB).filter(AgrovetDB.is_active == True).order_by(AgrovetDB.created_at.desc()).all()
        return [
            {
                "name": agrovet.business_name,
                "till_number": agrovet.till_number,
                "location": agrovet.location
            } for agrovet in agrovets
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# AGRI-CO AGENT ENDPOINTS
# ==========================================

@app.post("/api/v1/agents/register")
async def register_agent(request: AgentRegisterRequest, db: Session = Depends(get_db)):
    try:
        new_agent = AgentDB(agent_name=request.agent_name, phone_number=request.phone_number)
        db.add(new_agent)
        db.commit()
        return {"status": "success", "message": "Agent registered successfully"}
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Agent phone number is already registered.")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB Error: {str(e)}")

@app.post("/api/v1/agent/log-sale")
async def log_agent_sale(request: AgentSaleRequest, db: Session = Depends(get_db)):
    try:
        agent = db.query(AgentDB).filter(AgentDB.phone_number == request.agent_phone, AgentDB.is_active == True).first()
        if not agent:
            raise HTTPException(status_code=404, detail="Active Agent not found")

        farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == request.farmer_phone).first()
        if not farmer:
            raise HTTPException(status_code=404, detail="Farmer not registered on RahisiPay")

        commission = int(request.amount_kes * 0.05)
        receipt_code = f"AGRICO_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        new_tx = TransactionDB(
            mpesa_receipt=receipt_code,
            farmer_phone=request.farmer_phone,
            till_number="AGRICO_HUB",
            agent_phone=request.agent_phone,
            amount_kes=request.amount_kes,
            facility_fee=0 
        )
        
        agent.commission_balance += commission
        agent.total_sales_count += 1

        db.add(new_tx)
        db.commit()

        # Send confirmation SMS
        sms.send(f"Confirmed: You have purchased {request.product_name} via Agent {agent.agent_name}. KES {request.amount_kes} utilized.", [request.farmer_phone])

        return {
            "status": "success", 
            "receipt": receipt_code, 
            "commission_earned": commission,
            "new_balance": agent.commission_balance
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Log Sale Error: {str(e)}")

@app.get("/api/v1/agent/stats/{phone_number}")
async def get_agent_stats(phone_number: str, db: Session = Depends(get_db)):
    try:
        agent = db.query(AgentDB).filter(AgentDB.phone_number == phone_number).first()
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        return {
            "name": agent.agent_name,
            "balance": agent.commission_balance,
            "sales": agent.total_sales_count
        }
    except HTTPException as he:
        raise he  # Passes the 404 naturally
    except Exception as e:
        # Catches exact database mapping errors
        raise HTTPException(status_code=500, detail=f"Stats Error: {str(e)}")