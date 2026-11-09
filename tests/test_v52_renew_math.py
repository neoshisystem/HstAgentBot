from services.renewal_calculator import calculate_renewal

def test_overused_expired_and_onhold():
    current={"used_bytes":12*1024**3,"quota_bytes":10*1024**3,"expires_at":1_899_913_600,"quota_mode":"finite","time_mode":"dated"}
    plan={"quota_bytes":5*1024**3,"days":30,"quota_mode":"finite"}
    target=calculate_renewal(current,plan,"rollover_volume",1_900_000_000)
    assert target["used_bytes"]==12*1024**3
    assert target["quota_bytes"]==15*1024**3
    assert target["expires_at"]==1_900_000_000+30*86400
