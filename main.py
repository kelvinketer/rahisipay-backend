from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import os
import random # <-- NEW: For generating the 4-digit OTP
import africastalking # <-- NEW: SMS Engine
from datetime import datetime, timedelta

# --- SQLALCHEMY DATABASE IMPORTS ---
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime
from sqlalchemy.orm import sessionmaker, Session, declarative_base

# Initialize the RahisiPay API
app = FastAPI(title="RahisiPay Core API", version="4.0")

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

# --- TABLE 1: THE FARMER LEDGER ---
class FarmerDB(Base):
    __tablename__ = "farmers"
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True)
    crop_type = Column(String)
    farm_size_acres = Column(Float)
    trust_score = Column(Float)
    approved_limit = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

# --- TABLE 2: THE TRANSACTION LEDGER ---
class TransactionDB(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    mpesa_receipt = Column(String, unique=True, index=True)
    farmer_phone = Column(String)
    till_number = Column(String)
    amount_kes = Column(Integer)
    facility_fee = Column(Integer)
    timestamp = Column(DateTime, default=datetime.utcnow)

# --- NEW TABLE 3: THE OTP VAULT ---
class OTPStoreDB(Base):
    __tablename__ = "otp_store"
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, index=True)
    otp_code = Column(String)
    expires_at = Column(DateTime)

# Generate tables
Base.metadata.create_all(bind=engine)

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
    crop_type: str
    farm_size_acres: float
    repayment_history_multiplier: float = 1.0

class DisburseRequest(BaseModel):
    phone_number: str
    till_number: str
    amount_kes: int

# ==========================================
# CORE ALGORITHMS
# ==========================================
def calculate_score(crop: str, acres: float, history_mult: float):
    crop_scores = {"avocado": 10, "french_beans": 10, "coffee": 8, "maize": 4}
    c_score = crop_scores.get(crop.lower(), 3)
    
    if acres >= 5.0: f_score = 10
    elif acres >= 2.0: f_score = 7
    elif acres >= 0.5: f_score = 5
    else: f_score = 3
        
    base_score = (c_score * 0.6) + (f_score * 0.4)
    trust_score = min((base_score * 10) * history_mult, 100)
    
    if trust_score < 20: return {"tier": "Rejected", "amount": 0, "fee": 0, "score": trust_score}
    elif 20 <= trust_score <= 40: return {"tier": "Tier 1: Seed", "amount": 2500, "fee": 200, "score": trust_score}
    elif 41 <= trust_score <= 70: return {"tier": "Tier 2: Growth", "amount": 10000, "fee": 800, "score": trust_score}
    else: return {"tier": "Tier 3: Harvest", "amount": 50000, "fee": 4000, "score": trust_score}

def trigger_mpesa_b2b(amount: int, till_number: str, farmer_phone: str):
    transaction_ref = f"RAHISI_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return transaction_ref

# ==========================================
# SECURITY ENDPOINTS (OTP)
# ==========================================
@app.post("/api/v1/auth/send-otp")
async def send_otp(request: SendOTPRequest, db: Session = Depends(get_db)):
    try:
        # 1. Generate a secure 4-digit code
        code = str(random.randint(1000, 9999))
        
        # 2. Set expiration (10 minutes from now)
        expiry = datetime.utcnow() + timedelta(minutes=10)
        
        # 3. Save to database vault
        new_otp = OTPStoreDB(phone_number=request.phone_number, otp_code=code, expires_at=expiry)
        db.add(new_otp)
        db.commit()
        
        # 4. Dispatch SMS via Africa's Talking
        message = f"Welcome to Rahisi Agro Pay! Your verification code is: {code}. Do not share this PIN."
        sms.send(message, [request.phone_number])
        
        return {"status": "success", "message": "OTP sent successfully"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/auth/verify-otp")
async def verify_otp(request: VerifyOTPRequest, db: Session = Depends(get_db)):
    # 1. Find the most recent unexpired OTP for this number
    valid_otp = db.query(OTPStoreDB).filter(
        OTPStoreDB.phone_number == request.phone_number,
        OTPStoreDB.otp_code == request.otp_code,
        OTPStoreDB.expires_at > datetime.utcnow()
    ).first()
    
    if not valid_otp:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP code")
    
    # 2. Check if farmer already exists in the system to return their limits
    farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
    
    # 3. Delete the used OTP for security
    db.delete(valid_otp)
    db.commit()
    
    if farmer:
        return {"status": "success", "is_new_user": False, "score": farmer.trust_score, "limit": farmer.approved_limit}
    else:
        return {"status": "success", "is_new_user": True}

# ==========================================
# FINTECH ENDPOINTS
# ==========================================
@app.post("/api/v1/apply-loan")
async def apply_for_loan(request: LoanRequest, db: Session = Depends(get_db)):
    try:
        decision = calculate_score(request.crop_type, request.farm_size_acres, request.repayment_history_multiplier)
        
        farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
        if not farmer:
            farmer = FarmerDB(phone_number=request.phone_number, crop_type=request.crop_type, farm_size_acres=request.farm_size_acres, trust_score=decision["score"], approved_limit=decision["amount"])
            db.add(farmer)
        else:
            farmer.trust_score = decision["score"]
            farmer.approved_limit = decision["amount"]
            
        db.commit()
        return {"status": "success", "farmer": request.phone_number, "decision": decision}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/disburse")
async def disburse_funds(request: DisburseRequest, db: Session = Depends(get_db)):
    try:
        receipt_code = trigger_mpesa_b2b(request.amount_kes, request.till_number, request.phone_number)
        fee = int(request.amount_kes * 0.08)
        
        new_transaction = TransactionDB(mpesa_receipt=receipt_code, farmer_phone=request.phone_number, till_number=request.till_number, amount_kes=request.amount_kes, facility_fee=fee)
        db.add(new_transaction)
        db.commit()
        
        return {"status": "success", "disbursement": {"mpesa_receipt": receipt_code, "amount": request.amount_kes, "till_number": request.till_number}}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))