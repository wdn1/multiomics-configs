#!/usr/bin/env python

import os
import glob
import sys
import argparse
import logging
import itertools
import re

from pandas_schema import ValidationWarning
from sdrf_pipelines.zooma import ols
from sdrf_pipelines.sdrf import sdrf, sdrf_schema

DIR_LFQ = 'datasets/differential-datasets/label-free/'
DIR_TMT = 'datasets/differential-datasets/tmt/'
DIR_DIA = 'datasets/differential-datasets/dia/'
DIR_ABS = 'datasets/absolute-expression/'

def get_files_sdrf():
  lfq = glob.glob(DIR_LFQ + '**/*.sdrf.tsv')
  tmt = glob.glob(DIR_TMT + '**/*.sdrf.tsv')
  dia = glob.glob(DIR_DIA + '**/*.sdrf.tsv')
  abs = glob.glob(DIR_ABS + '**/**/*.sdrf.tsv')
  return lfq + tmt + dia + abs

PROJECTS = os.listdir(DIR_LFQ)

client = ols.OlsClient()

def retry(func):
    def wrapper(*args, **kwargs):
        for i in range(5):
            try:
                return func(*args, **kwargs)
            except Exception:
                pass
    return wrapper


@retry
def get_ancestors(iri):
    return client.get_ancestors('ncbitaxon', iri)


def organism_name(s):
    m = re.search(r'nt=([^;]*)', s)
    if m:
        name = m.group(1)
    else:
        name = s
    return name


def get_template(df):
    """Extract organism information and pick a template for validation"""
    templates = []
    cell = 'characteristics[cell line]'

    if cell in df:
        is_cell_line = ~df[cell].isin({'not applicable', 'not available'})
        if is_cell_line.any():
            templates.append(sdrf_schema.CELL_LINES_TEMPLATE)
            df = df.loc[~is_cell_line]

    organisms = df['characteristics[organism]'].unique()

    for org in organisms:
        org = organism_name(org)
        if org == 'homo sapiens':
            templates.append(sdrf_schema.HUMAN_TEMPLATE)
        else:
            hit = client.besthit(org, ontology='ncbitaxon')
            if hit is not None:
                iri = hit['iri']
                ancestors = get_ancestors(iri)
                if ancestors is None:
                    print('Could not get ancestors for {}!'.format(org))
                    ancestors = []
                labels = {a['label'] for a in ancestors}
                if 'Gnathostomata <vertebrates>' in labels:
                    templates.append(sdrf_schema.VERTEBRATES_TEMPLATE)
                elif 'Metazoa' in labels:
                    templates.append(sdrf_schema.NON_VERTEBRATES_TEMPLATE)
                elif 'Viridiplantae' in labels:
                    templates.append(sdrf_schema.PLANTS_TEMPLATE)
    return templates


def is_error(err):
    if hasattr(err, '_error_type'):
        return err._error_type == logging.ERROR
    if not isinstance(err, ValidationWarning):
        raise TypeError('Validation errors should be of type ValidationWarning, not {}'.format(type(err)))
    return True


def is_warning(err):
    if hasattr(err, '_error_type'):
        return err._error_type == logging.WARN
    if not isinstance(err, ValidationWarning):
        raise TypeError('Validation errors should be of type ValidationWarning, not {}'.format(type(err)))
    return False


def has_errors(errors):
    return any(is_error(err) for err in errors)


def has_warnings(errors):
    return any(is_warning(err) for err in errors)


def collapse_warnings(errors):
    warnings = [err for err in errors if is_warning(err)]
    messages = []
    if warnings:
        key = lambda w: (w.column, w.message)
        for keyv, group in itertools.groupby(sorted(warnings, key=key), key=key):
            col, message = keyv
            gr = list(group)
            w = min(gr, key=lambda w: w.row)
            messages.append('{} validation warnings collapsed on column {} (first row {}, value {}): {}'.format(
                len(gr), col, w.row, w.value, message))
    return messages

def remove_biological_replicates(errors):
  err_result = []
  for err in errors:
   if 'biological replicate' not in err.message and 'technical replicate' not in err.message:
     err_result.append(err)
  return err_result

def main(args):
    statuses = []
    messages = []
    if args.project:
        sdrf_files = [args.project[1]]
    else:
        sdrf_files = get_files_sdrf()
    i = 0
    try:
      for sdrf_file in sdrf_files:
        error_types = set()
        error_files = set()
        status = 0
        templates = []
        result = 'OK'
        errors = []
        try:
          df = sdrf.SdrfDataFrame.parse(sdrf_file)
          err = df.validate(sdrf_schema.DEFAULT_TEMPLATE)
          err = remove_biological_replicates(err)
          errors.extend(err)
          if has_errors(err):
            error_types.add('basic')
          else:
            templates = get_template(df)
            if templates:
              for t in templates:
                err = df.validate(t)
                err = remove_biological_replicates(err)
                errors.extend(err)
                if has_errors(err):
                  error_types.add('{} template'.format(t))
            err = df.validate(sdrf_schema.MASS_SPECTROMETRY)
            err = remove_biological_replicates(err)
            errors.extend(err)
            if has_errors(err):
              error_types.add('mass spectrometry')
          if has_errors(errors):
            error_files.add(os.path.basename(sdrf_file))
        except:
          print(sdrf_file)
        if error_types:
          result = 'Failed ' + ', '.join(error_types) + ' validation ({})'.format(', '.join(error_files))
          status = 2
        elif has_warnings(errors):
          result = 'OK (with warnings)'
          status = 1
        if status < 2:
          result = '[{} template]\t'.format(', '.join(templates) if templates else 'default') + result
        statuses.append(status)
        messages.append(result)
        if args.verbose == 2:
          for err in errors:
            print(err)
        elif args.verbose:
          for w in collapse_warnings(errors):
            print(w)
          for err in errors:
            if is_error(err):
              print(err)
        print(sdrf_file, result, sep='\t')
        i += 1
    except KeyboardInterrupt:
        pass
    finally:
        errors = sum(s == 2 for s in statuses)
        warnings = sum(s == 1 for s in statuses)
        print('Final results:')
        print(f'Total: {i} of {len(sdrf_files)} projects checked, '
              f'{errors} had validation errors, {warnings} had validation warnings.')
    return errors


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='count', help='Print all errors. If specified twice, print all warnings.')
    parser.add_argument('project', nargs='*')
    args = parser.parse_args()
    out = main(args)
    sys.exit(out)
