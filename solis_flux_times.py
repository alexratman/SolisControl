from datetime import time

import solis_control_req_mod as solis_control
import solis_common as common

cron_before = pyscript.app_config.get('cron_before', 20) # integer
charge_start_hhmm = pyscript.app_config['solis_control']['charge_period']['start'] # HH:MM string
discharge_start_hhmm = pyscript.app_config['solis_control']['discharge_period']['start'] # HH:MM string
charge_start_time = time.fromisoformat(charge_start_hhmm+':00') # start of morning cheap charging
discharge_start_time = time.fromisoformat(discharge_start_hhmm+':00') # start of evening peak discharging
charge_start_time = common.time_adjust(charge_start_time, -cron_before) # time to run before before charge period
discharge_start_time = common.time_adjust(discharge_start_time, -cron_before) # time to run before discharge period
charge_start_cron = "%d %d * * *" % (charge_start_time.minute, charge_start_time.hour)
discharge_start_cron = "%d %d * * *" % (discharge_start_time.minute, discharge_start_time.hour)
n_forecasts = 7 # number of old solar forecasts to store
log_msg = 'Current energy %.1fkWh (%.0f%%) -> set %s from %s to %s to reach %.1fkWh target'
log_err_msg = 'Current energy %.1fkWh (%.0f%%) -> error setting %s from %s to %s to reach %.1fkWh target -> %s'

def sensor_get(entity_name): # sensor must exist
    entity_name = entity_name if entity_name.startswith('sensor.') else 'sensor.' + entity_name
    result = state.get(entity_name)
    if result in ['unavailable', 'unknown', 'none']:
        return None
    return result
        
def pyscript_get(entity_name): # creates persistent pyscript state variable if it doesn't exist
    try:
        result = state.get(entity_name)
        if result in ['unavailable', 'unknown', 'none']:
            return None
        return result
    except NameError:
        state.persist(entity_name, default_value='') # comma separated list of last n_forecasts
        return ''

def get_forecast(forecast_type=None, save=False):
    # get the solar forecast (in kWh) or if not available use average of last n_forecasts
    forecast = sensor_get(pyscript.app_config['forecast_remaining'])
    if not forecast_type:
        return None if forecast is None else float(forecast)
    old_forecasts = 'pyscript.' +forecast_type+'_forecasts'
    lf = pyscript_get(old_forecasts)
    if lf:
        lf = lf.split(sep=',')
        lf = [ float(f) for f in lf ]
    else:
        lf = []
    if forecast is None:
        forecast = sum(lf) / len(lf) if lf else 0.0 # use average of old forecasts if current solar power forecast not available
    else:
        forecast = float(forecast)
        if save:
            lf.append(forecast)         # add new forecast to right side of list
            lf = lf[-n_forecasts:]      # maxlen = n_forecasts
            lf = [ '{:.1f}'.format(f) for f in lf ]
            state.set(old_forecasts, value=','.join(lf))
    return forecast

def calc_level(required, forecast=None, forecast_type=''):
    # if necessary reduce the required energy level by the predicted solar forecast
    level = required - forecast # target energy level in battery to meet requirement
    log.info('Energy required %.1fkWh - solar %s forecast %.1fkWh = target %.1fkWh', required, forecast_type, forecast, level)
    return level

@time_trigger("cron(" + charge_start_cron + ")")
def set_charge_times():
    forecast = get_forecast('morning', save=True)
    required = pyscript.app_config['morning_requirement']
    level_adjusted = calc_level(required, forecast, 'morning') if forecast else required
    result = set_times('charge', level_adjusted, test=False)
    if result != 'OK':
        task.sleep(5 * 60) # try again once after 5 mins
        set_times('charge', level_adjusted, test=False)
            
@time_trigger("cron(" + discharge_start_cron + ")")
def set_discharge_times():
    forecast = get_forecast('evening', save=True)
    required = pyscript.app_config['evening_requirement']
    level_adjusted = calc_level(required, forecast, 'evening') if forecast else required
    result = set_times('discharge', level_adjusted, test=False)
    if result != 'OK':
        task.sleep(5 * 60) # try again once after 5 mins
        set_times('discharge', level_adjusted, test=False)

def set_times(action, level_required, test=True):
    if action not in ('charge', 'discharge'):
        log.warning('Invalid action: ' + action)
        return
    with solis_control.get_session() as session:
        config = dict(pyscript.app_config['solis_control'])
        connected = solis_control.connect(config, session)
        if connected:
            unavailable_energy, full_energy, current_energy, real_soc = common.energy_values(config)
            if action == "charge":
                start, end = common.charge_times(config, level_required) # discharge times to reach required energy level
                if test:
                    result = common.check_all(config) # check time sync and current settings only
                else:
                    result = solis_control.set_inverter_times(config, session, charge_start = start, charge_end = end)
            elif action == "discharge":
                start, end = common.discharge_times(config, level_required) # discharge times to reach required energy level
                if test:
                    result = common.check_all(config) # check time sync and current settings only
                else:
                    result = solis_control.set_inverter_times(config, session, discharge_start = start, discharge_end = end)
            log_action = 'notional ' + action if test else action
            if result == 'OK':
                log.info(log_msg, current_energy, real_soc, log_action, start, end, level_required)
            else:
                log.error(log_err_msg, current_energy, real_soc, log_action, start, end, level_required, result)
        else:
            log.error('Could not connect to Solis API')
            
@service
def test(action=None, level_required=None, use_forecast=False):
    """yaml
name: Test service
description: Tests connection to the Solis API and calculates what the charge and discharge times would be set
fields:
  action:
     description: set either charge (morning/cheap) or discharge (evening/peak) times
     example: charge
     required: true
     selector:
       select:
         options:
           - charge
           - discharge
  level_required:
     description: target energy level (kWh) available for use after charge or discharge period - if not specified uses the morning/evening_requirement values in the configuration
     example: 5.0
     required: false
  use_forecast:
     description: whether to subtract the solar forecast remaining today from the level_required value
     example: true
     required: false
     default: false
"""
    if not level_required:
        if action == "charge":
            level_required = pyscript.app_config['morning_requirement']
        elif action == "discharge":
            level_required = pyscript.app_config['evening_requirement']
    if level_required:
        if use_forecast:
            if action == "charge":
                forecast_type = 'morning'
            elif action == "discharge":
                forecast_type = 'evening'
            forecast = get_forecast(forecast_type, save=False)
            if forecast:
                level_required = calc_level(level_required, forecast, forecast_type)
        set_times(action, level_required, test=True)


