import os
import google.generativeai as genai
from fastapi import FastAPI, HTTPException, Depends, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import requests
import random 
import africastalking 
from datetime import datetime, timedelta

# --- CLOUDINARY IMPORT & CONFIG ---
import cloudinary
import cloudinary.uploader
import cloudinary.api

cloudinary.config( 
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"), 
    api_key = os.getenv("CLOUDINARY_API_KEY"), 
    api_secret = os.getenv("CLOUDINARY_API_SECRET") 
)

# --- SQLALCHEMY DATABASE IMPORTS ---
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

print(f"🚀 STARTING AFRICA'S TALKING WITH USERNAME: {AT_USERNAME}")

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
    
    # --- NEW: DIGITAL COOPERATIVE INTEGRATION ---
    group_id = Column(Integer, index=True, nullable=True) # Links to FarmerGroupDB.id
    is_group_leader = Column(Boolean, default=False)      # Flags if they are the hub-and-spoke trainer

# --- NEW: FARMER COOPERATIVE GROUPS TABLE ---
class FarmerGroupDB(Base):
    __tablename__ = "farmer_groups"
    id = Column(Integer, primary_key=True, index=True)
    group_name = Column(String, index=True)             # e.g., "Juja Youth Poultry Co-op"
    region = Column(String)                             # e.g., "Kiambu"
    leader_phone = Column(String)                       # Phone number of the localized trainer/leader
    collective_trust_score = Column(Float, default=0.0) # The group's multiplier based on collective repayment
    member_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)

class TransactionDB(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    mpesa_receipt = Column(String, unique=True, index=True)
    farmer_phone = Column(String)
    till_number = Column(String)
    agent_phone = Column(String, nullable=True)
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

class AgentDB(Base):
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String)
    phone_number = Column(String, unique=True, index=True)
    commission_balance = Column(Integer, default=0) 
    total_sales_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

# --- CITIZEN SCIENCE RESEARCH DATABASE ---
class AIDiagnosisDB(Base):
    __tablename__ = "ai_diagnoses"
    id = Column(Integer, primary_key=True, index=True)
    farmer_phone = Column(String, index=True)
    image_url = Column(String, nullable=True) 
    diagnosis_text = Column(String)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ==========================================
# AUTO-MIGRATIONS FOR DATABASE UPDATES
# ==========================================
try:
    with engine.connect() as conn:
        # Existing Migrations
        conn.execute(text("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS agent_phone VARCHAR"))
        conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS commission_balance INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_sales_count INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE agents ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
        
        # --- NEW COOPERATIVE MIGRATIONS ---
        conn.execute(text("ALTER TABLE farmers ADD COLUMN IF NOT EXISTS group_id INTEGER"))
        conn.execute(text("ALTER TABLE farmers ADD COLUMN IF NOT EXISTS is_group_leader BOOLEAN DEFAULT FALSE"))
        
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

# --- NEW: ACADEMY REWARD MODEL ---
class AcademyCompletionRequest(BaseModel):
    phone_number: str
    course_name: str
    score_boost: float = 2.0  # +2% trust score
    limit_boost: int = 5000   # + KES 5,000 buying power

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

class AgrovetLoginRequest(BaseModel):
    till_number: str

class AgrovetVerifyRequest(BaseModel):
    till_number: str
    otp_code: str

class AgentRegisterRequest(BaseModel):
    agent_name: str
    phone_number: str

class AgentSaleRequest(BaseModel):
    agent_phone: str
    farmer_phone: str
    amount_kes: int
    product_name: str 

class ChatRequest(BaseModel):
    farmer_phone: str
    message: str

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
        
        print(f"--- 📡 INITIATING SMS TO {request.phone_number} ---")
        at_response = sms.send(f"Welcome to Oletai Bank! Your PIN: {code}", [request.phone_number])
        print(f"🔥 AFRICA'S TALKING RAW RESPONSE: {at_response}")
        
        return {"status": "success", "message": "OTP processing", "at_debug": at_response}
        
    except Exception as e:
        db.rollback()
        print(f"❌ CRASH IN SMS MODULE: {str(e)}")
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

# --- NEW: ACADEMY REWARD ENDPOINT ---
@app.post("/api/v1/academy/complete", tags=["Academy"])
async def complete_academy_module(request: AcademyCompletionRequest, db: Session = Depends(get_db)):
    try:
        user = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
        if not user:
            raise HTTPException(status_code=404, detail="Farmer not found")

        # 1. Apply the Learn-to-Earn Rewards
        # Cap the trust score at 100%
        user.trust_score = min(user.trust_score + request.score_boost, 100.0)
        user.approved_limit += request.limit_boost

        # 2. Log the transaction as an "Education Bonus"
        reward_ref = f"ACADEMY_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        new_tx = TransactionDB(
            mpesa_receipt=reward_ref, 
            farmer_phone=request.phone_number, 
            till_number="OLETAI_ACADEMY", 
            amount_kes=request.limit_boost, 
            facility_fee=0
        )
        
        db.add(new_tx)
        db.commit()

        print(f"🎓 Academy Reward Applied! {request.phone_number} earned {request.limit_boost} KES.")

        return {
            "status": "success", 
            "new_score": user.trust_score,
            "new_limit": user.approved_limit
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/transactions/{phone_number}")
async def get_transaction_history(phone_number: str, db: Session = Depends(get_db)):
    history = db.query(TransactionDB).filter(TransactionDB.farmer_phone == phone_number).order_by(TransactionDB.timestamp.desc()).all()
    return [
        {
            "id": tx.id,
            "title": "Education Bonus" if tx.till_number == "OLETAI_ACADEMY" else ("Loan Repayment" if tx.till_number == "OLETAI_BANK" else f"Payment (Till {tx.till_number})"),
            "date": tx.timestamp.strftime("%b %d, %I:%M %p"),
            "amount": f"{'+' if (tx.till_number == 'OLETAI_BANK' or tx.till_number == 'OLETAI_ACADEMY') else '-'} KES {tx.amount_kes}",
            "is_credit": tx.till_number == "OLETAI_BANK" or tx.till_number == "OLETAI_ACADEMY"
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

@app.post("/api/v1/agrovets/login/send-otp", tags=["Agrovet Auth"])
async def agrovet_send_otp(request: AgrovetLoginRequest, db: Session = Depends(get_db)):
    agrovet = db.query(AgrovetDB).filter(AgrovetDB.till_number == request.till_number).first()
    if not agrovet:
        raise HTTPException(status_code=404, detail="Till Number not found. Please register first.")
    
    code = str(random.randint(1000, 9999))
    expiry = datetime.utcnow() + timedelta(minutes=10)
    
    new_otp = OTPStoreDB(phone_number=agrovet.owner_phone, otp_code=code, expires_at=expiry)
    db.add(new_otp)
    db.commit()
    
    print(f"--- 📡 INITIATING AGROVET SMS TO {agrovet.owner_phone} ---")
    at_response = sms.send(f"Oletai Merchant Portal PIN: {code}", [agrovet.owner_phone])
    print(f"🔥 AFRICA'S TALKING RAW RESPONSE: {at_response}")
    
    masked_phone = f"{agrovet.owner_phone[:5]}***{agrovet.owner_phone[-4:]}"
    return {"status": "success", "masked_phone": masked_phone}

@app.post("/api/v1/agrovets/login/verify-otp", tags=["Agrovet Auth"])
async def agrovet_verify_otp(request: AgrovetVerifyRequest, db: Session = Depends(get_db)):
    agrovet = db.query(AgrovetDB).filter(AgrovetDB.till_number == request.till_number).first()
    if not agrovet:
        raise HTTPException(status_code=404, detail="Agrovet not found")
        
    valid_otp = db.query(OTPStoreDB).filter(
        OTPStoreDB.phone_number == agrovet.owner_phone, 
        OTPStoreDB.otp_code == request.otp_code, 
        OTPStoreDB.expires_at > datetime.utcnow()
    ).first()
    
    if not valid_otp:
        raise HTTPException(status_code=400, detail="Invalid or expired PIN")
        
    db.delete(valid_otp)
    db.commit()
    
    return {
        "status": "success", 
        "business_name": agrovet.business_name,
        "owner_phone": agrovet.owner_phone,
        "till_number": agrovet.till_number
    }

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

        print(f"--- 📡 INITIATING CONFIRMATION SMS TO {request.farmer_phone} ---")
        at_response = sms.send(f"Confirmed: You have purchased {request.product_name} via Agent {agent.agent_name}. KES {request.amount_kes} utilized.", [request.farmer_phone])
        print(f"🔥 AFRICA'S TALKING RAW RESPONSE: {at_response}")

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
        print(f"❌ CRASH IN LOG SALE MODULE: {str(e)}")
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
        raise he 
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stats Error: {str(e)}")
    
# ==========================================
# OLETAI AI FARM ADVISOR (POWERED BY GEMINI)
# ==========================================

# Configure Gemini securely
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("❌ ERROR: GEMINI_API_KEY is missing from Render Environment Variables!")
else:
    print("✅ GEMINI_API_KEY detected. Initializing AI Engine...")
    genai.configure(api_key=GEMINI_API_KEY)

@app.post("/api/v1/advisor/chat", tags=["AI Advisor"])
async def chat_with_advisor(req: ChatRequest, db: Session = Depends(get_db)):
    try:
        if not GEMINI_API_KEY:
            return {"status": "error", "reply": "AI service is currently misconfigured."}
            
        # 1. FETCH LIVE CONTEXT FROM DATABASE
        farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == req.farmer_phone).first()
        
        trust_score = farmer.trust_score if farmer else "0 (New User)"
        buying_power = farmer.approved_limit if farmer else "0"
        
        # 2. BUILD THE DYNAMIC SYSTEM PROMPT
        dynamic_instruction = f"""
        You are the 'Oletai Farm Advisor', an expert agronomist operating in Kenya. 
        Your tone is professional and encouraging. Always refer to capital as an 'investment'.
        
        CRITICAL ACCOUNT CONTEXT:
        - The farmer you are talking to has an Agri-Trust Score of: {trust_score}%
        - They currently have an available Buying Power of: KES {buying_power}
        
        RULES:
        1. If they ask about funding, loans, capital, or how much money they have, DO NOT tell them to visit a website or contact support.
        2. Instead, explicitly tell them they have KES {buying_power} available right now. Tell them to click the 'Invest' button on their dashboard to use it at a local Agrovet.
        3. Keep your advice practical, localized to Kenya, and concise.
        """
        
        # 3. INITIALIZE THE MODEL WITH DYNAMIC INSTRUCTIONS
        ai_model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=dynamic_instruction
        )

        # 4. GENERATE RESPONSE
        response = ai_model.generate_content(req.message)
        return {"status": "success", "reply": response.text}
        
    except Exception as e:
        print(f"🔥 GEMINI CRASH: {str(e)}")
        raise HTTPException(status_code=500, detail="AI Engine Error")

# --- UPDATED: MULTIMODAL DIAGNOSIS ENDPOINT (CLOUD STORAGE + GPS) ---
@app.post("/api/v1/advisor/diagnose", tags=["AI Advisor"])
async def diagnose_crop_issue(
    farmer_phone: str = Form(...),
    latitude: Optional[float] = Form(None),   
    longitude: Optional[float] = Form(None),  
    image: UploadFile = File(...),
    db: Session = Depends(get_db)             
):
    try:
        if not GEMINI_API_KEY:
            return {"status": "error", "diagnosis": "AI service is currently misconfigured."}

        image_bytes = await image.read()
        
        # 1. UPLOAD IMAGE TO CLOUDINARY
        secure_image_url = None
        try:
            upload_result = cloudinary.uploader.upload(image_bytes)
            secure_image_url = upload_result.get("secure_url")
            print(f"☁️ Image successfully uploaded to Cloudinary: {secure_image_url}")
        except Exception as cloud_err:
            print(f"⚠️ Warning: Cloudinary upload failed: {str(cloud_err)}")

        # 2. ASK GEMINI FOR DIAGNOSIS
        prompt = """
        Analyze this image of a crop or livestock. 
        1. Identify the species if possible.
        2. Detect any visible signs of disease, pests, or malnutrition.
        3. Provide a clear diagnosis and a step-by-step 'Investment Plan' to fix it.
        4. Mention if specific inputs (pesticides/fertility) are available at Oletai Agrovets.
        Keep it professional and localized to Kenya.
        """
        
        vision_model = genai.GenerativeModel(model_name="gemini-2.5-flash")
        response = vision_model.generate_content([
            prompt,
            {"mime_type": image.content_type, "data": image_bytes}
        ])
        
        diagnosis_result = response.text

        # 3. SAVE THE COMPLETE RECORD TO POSTGRESQL
        try:
            new_diagnosis = AIDiagnosisDB(
                farmer_phone=farmer_phone,
                diagnosis_text=diagnosis_result,
                latitude=latitude,
                longitude=longitude,
                image_url=secure_image_url 
            )
            db.add(new_diagnosis)
            db.commit()
            print(f"✅ Saved full AI Diagnosis record for {farmer_phone} to Database.")
        except Exception as db_err:
            db.rollback()
            print(f"⚠️ Warning: Failed to save diagnosis to DB: {str(db_err)}")

        return {"status": "success", "diagnosis": diagnosis_result}
        
    except Exception as e:
        print(f"🔥 DIAGNOSIS CRASH: {str(e)}")
        raise HTTPException(status_code=500, detail="AI Diagnostic Error")
    
    # ==========================================
# 7. DIGITAL COOPERATIVES (COMMUNITY) ENDPOINTS
# ==========================================
class JoinCommunityRequest(BaseModel):
    phone_number: str
    invite_code: str

@app.get("/api/v1/community/status/{phone_number}", tags=["Community"])
async def get_community_status(phone_number: str, db: Session = Depends(get_db)):
    try:
        farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == phone_number).first()
        if not farmer:
            raise HTTPException(status_code=404, detail="Farmer not found")

        # If they don't have a group ID, return false
        if not farmer.group_id:
            return {"has_group": False}

        # If they do, fetch the group details
        group = db.query(FarmerGroupDB).filter(FarmerGroupDB.id == farmer.group_id).first()
        if not group:
            return {"has_group": False}

        return {
            "has_group": True,
            "group_name": group.group_name,
            "region": group.region,
            "member_count": group.member_count,
            "collective_trust_score": group.collective_trust_score,
            "leader_phone": group.leader_phone
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/community/join", tags=["Community"])
async def join_community(request: JoinCommunityRequest, db: Session = Depends(get_db)):
    try:
        farmer = db.query(FarmerDB).filter(FarmerDB.phone_number == request.phone_number).first()
        if not farmer:
            raise HTTPException(status_code=404, detail="Farmer not found")

        # MVP Hack: Hardcode an invite code to auto-generate our test group
        if request.invite_code.upper() == "GROW2026":
            group = db.query(FarmerGroupDB).filter(FarmerGroupDB.group_name == "Nairobi Youth Poultry Co-op").first()
            
            # Create the group if it doesn't exist yet in the database
            if not group:
                group = FarmerGroupDB(
                    group_name="Nairobi Youth Poultry Co-op",
                    region="Nairobi, Kenya",
                    leader_phone="0700000000",
                    collective_trust_score=85.0,
                    member_count=23 # Start at 23, the user joining makes it 24
                )
                db.add(group)
                db.commit()
                db.refresh(group)

            # Assign farmer to the group and increment the count
            farmer.group_id = group.id
            group.member_count += 1
            db.commit()

            return {"status": "success", "message": f"Welcome to {group.group_name}!"}
        else:
            raise HTTPException(status_code=400, detail="Invalid Invite Code. Try GROW2026.")
            
    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))