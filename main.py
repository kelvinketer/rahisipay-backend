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
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime
from sqlalchemy.orm import sessionmaker, Session, declarative_base

# Initialize the Oletai Agri Finance Core API
app = FastAPI(title="Oletai Agri Finance Bank Core API", version="7.0")

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
    crop_type = Column(String)
    farm_size_acres = Column(Float)
    trust_score = Column(Float)
    approved_limit = Column(Integer)
    country_code = Column(String, default="KE")
    preferred_language = Column(String, default="en")
    base_currency = Column(String, default="KES")
    measurement_unit = Column(String, default="acres")
    national_id_type = Column(String, nullable=True)
    id_document_url = Column(String, nullable=True)
    email_address = Column(String, nullable=True)
    region_climate_zone = Column(String, default="East African Highlands")
    created_at = Column(DateTime, default=datetime.utcnow)

class TransactionDB(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    mpesa_receipt = Column(String, unique=True, index=True)
    farmer_phone = Column(String)
    till_number = Column(String)
    amount_kes = Column(Integer)
    facility_fee = Column(Integer)
    timestamp = Column(DateTime, default=datetime.utcnow)

class OTPStoreDB(Base):
    __tablename__ = "otp_store"
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, index=True)
    otp_code = Column(String)
    expires_at = Column(DateTime)

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
    country_code: str = "KE"
    preferred_language: str = "en"
    base_currency: str = "KES"
    measurement_unit: str = "acres"
    email_address: Optional[str] = None
    region_climate_zone: str = "East African Highlands"

class DisburseRequest(BaseModel):
    phone_number: str
    till_number: str
    amount_kes: int

class RepayRequest(BaseModel):
    phone_number: str
    amount: int

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

# ==========================================
# ENDPOINTS
# ==========================================

@app.post("/api/v1/auth/send-otp")
async def send_otp(request: SendOTPRequest, db: Session = Depends(get_db)):
    try:
        code = str(random.randint(1000, 9999))
        expiry = datetime.utcnow() + timedelta(minutes=10)
        new_otp = OTPStoreDB(phone_number=request.phone_number, otp_code=code, expires_at=expiry)
        db.add(new_otp)
        db.commit()
        sms.send(f"Welcome to Oletai Agri Finance! Code: {code}", [request.phone_number])
        return {"status": "success", "message": "OTP sent successfully"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/auth/verify-otp")
async def verify_otp(request: VerifyOTPRequest, db: Session = Depends(get_db)):
    valid_otp = db.query(OTPStoreDB).filter(OTPStoreDB.phone_number == request.phone_number, OTPStoreDB.otp_code == request.otp_code, OTPStoreDB.expires_at > datetime.utcnow()).first()
    if not valid_otp:
        raise HTTPException(status_code=400, detail="Invalid PIN")
    farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
    db.delete(valid_otp)
    db.commit()
    return {"status": "success", "is_new_user": farmer is None, "score": farmer.trust_score if farmer else 0, "limit": farmer.approved_limit if farmer else 0}

@app.get("/api/v1/transactions/{phone_number}")
async def get_transaction_history(phone_number: str, db: Session = Depends(get_db)):
    history = db.query(TransactionDB).filter(TransactionDB.farmer_phone == phone_number).order_by(TransactionDB.timestamp.desc()).all()
    return [
        {
            "id": tx.id,
            "title": "Loan Repayment" if tx.till_number == "OLETAI_BANK" else f"Agrovet Payment (Till {tx.till_number})",
            "date": tx.timestamp.strftime("%b %d, %I:%M %p"),
            "amount": f"{'+' if tx.till_number == 'OLETAI_BANK' else '-'} KES {tx.amount_kes}",
            "is_credit": tx.till_number == "OLETAI_BANK"
        } for tx in history
    ]

@app.post("/api/v1/apply-loan")
async def apply_for_loan(request: LoanRequest, db: Session = Depends(get_db)):
    decision = calculate_score(request.crop_type, request.farm_size_acres, request.repayment_history_multiplier)
    farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
    if not farmer:
        farmer = FarmerDB(phone_number=request.phone_number, crop_type=request.crop_type, farm_size_acres=request.farm_size_acres, trust_score=decision["score"], approved_limit=decision["amount"])
        db.add(farmer)
    else:
        farmer.trust_score = decision["score"]
        farmer.approved_limit = decision["amount"]
    db.commit()
    return {"status": "success", "decision": decision}

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
        farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
        if not farmer:
            raise HTTPException(status_code=404, detail="Farmer not found")
        
        # Record Repayment
        repayment_ref = f"REPAY_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        new_tx = TransactionDB(mpesa_receipt=repayment_ref, farmer_phone=request.phone_number, till_number="OLETAI_BANK", amount_kes=request.amount, facility_fee=0)
        db.add(new_tx)

        # Boost Trust Score (Rewards positive behavior)
        farmer.trust_score = min(farmer.trust_score + 5.0, 100.0)
        db.commit()

        return {"status": "success", "message": f"Repayment of KES {request.amount} confirmed.", "new_score": farmer.trust_score}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))