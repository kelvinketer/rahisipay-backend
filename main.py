from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
from datetime import datetime

# --- NEW: SQLALCHEMY DATABASE IMPORTS ---
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime
from sqlalchemy.orm import sessionmaker, Session, declarative_base

# Initialize the RahisiPay API
app = FastAPI(title="RahisiPay Core API", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# DATABASE ARCHITECTURE
# ==========================================
SQLALCHEMY_DATABASE_URL = "sqlite:///./rahisipay.db"

# Create the SQLite engine
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
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

# Generate the tables in the database file
Base.metadata.create_all(bind=engine)

# Dependency to get the database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================================
# DATA MODELS (FLUTTER TO PYTHON)
# ==========================================
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
    print(f"\n[DARAJA API ALERT] B2B Transfer Executed -> Ref: {transaction_ref}")
    return transaction_ref


# ==========================================
# API ENDPOINTS (WITH DATABASE SAVING)
# ==========================================
@app.post("/api/v1/apply-loan")
async def apply_for_loan(request: LoanRequest, db: Session = Depends(get_db)):
    try:
        # 1. Run the scoring algorithm
        decision = calculate_score(request.crop_type, request.farm_size_acres, request.repayment_history_multiplier)
        
        # 2. Check if farmer exists, otherwise create them
        farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
        if not farmer:
            farmer = FarmerDB(
                phone_number=request.phone_number,
                crop_type=request.crop_type,
                farm_size_acres=request.farm_size_acres,
                trust_score=decision["score"],
                approved_limit=decision["amount"]
            )
            db.add(farmer)
        else:
            # Update existing farmer's limit
            farmer.trust_score = decision["score"]
            farmer.approved_limit = decision["amount"]
            
        # 3. Save to database
        db.commit()
        
        return {"status": "success", "farmer": request.phone_number, "decision": decision}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/disburse")
async def disburse_funds(request: DisburseRequest, db: Session = Depends(get_db)):
    try:
        # 1. Trigger the Safaricom payment
        receipt_code = trigger_mpesa_b2b(request.amount_kes, request.till_number, request.phone_number)
        
        # 2. Calculate the 8% fee
        fee = int(request.amount_kes * 0.08)
        
        # 3. Record the transaction in the Ledger
        new_transaction = TransactionDB(
            mpesa_receipt=receipt_code,
            farmer_phone=request.phone_number,
            till_number=request.till_number,
            amount_kes=request.amount_kes,
            facility_fee=fee
        )
        db.add(new_transaction)
        db.commit()
        
        return {
            "status": "success", 
            "disbursement": {
                "mpesa_receipt": receipt_code,
                "amount": request.amount_kes,
                "till_number": request.till_number
            }
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    