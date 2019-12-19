import jax
import jax.lax as lax
import jax.scipy.special as spec
import jax.numpy as np
import numpy as np0
import scipy.sparse as sp

from .design import design_matrices
from .summary import param_table

##
## constants
##

# numbers
eps = 1e-7
clip_like = 20.0

# polygamma functions
@jax.custom_transforms
def trigamma(x):
    return 1/x + 1/(2*x**2) + 1/(6*x**3) - 1/(30*x**5) + 1/(42*x**7) - 1/(30*x**9) + 5/(66*x**11) - 691/(2730*x**13) + 7/(6*x**15)

@jax.custom_transforms
def digamma(x):
    return spec.digamma(x)

@jax.custom_transforms
def gammaln(x):
    return spec.gammaln(x)

jax.defjvp(digamma, lambda g, y, x: lax.mul(g, trigamma(x)))
jax.defjvp(gammaln, lambda g, y, x: lax.mul(g, digamma(x)))

def sigmoid(x):
    return 1/(1+np.exp(-x))

# link functions
links = {
    'identity': lambda x: x,
    'exponential': lambda x: np.exp(x),
    'logit': lambda x: 1/(1+np.exp(-x))
}

# loss functions
losses = {
    'binary': lambda yh, y: y*np.log(yh) + (1-y)*np.log(1-yh),
    'poisson': lambda yh, y: y*np.log(yh) - yh,
    'negative_binomial': lambda r, yh, y: gammaln(r+y) - gammaln(r) + r*np.log(r) + y*np.log(yh) - (r+y)*np.log(r+yh),
    'least_squares': lambda yh, y: -(y-yh)**2
}

##
## batching it
##

class DataLoader:
    def __init__(self, y, x, batch_size):
        self.y = y
        self.x = x
        self.batch_size = batch_size
        self.data_size = len(y)
        self.num_batches = self.data_size // batch_size
        self.sparse = sp.issparse(x)

    def __iter__(self):
        loc = 0
        for i in range(self.num_batches):
            by, bx = self.y[loc:loc+self.batch_size], self.x[loc:loc+self.batch_size, :]
            if self.sparse:
                bx = bx.toarray()
            yield by, bx
            loc += self.batch_size

##
## estimation
##

# maximum likelihood using jax - this expects a mean log likelihood
def maxlike(y, x, model, params0, batch_size=4092, epochs=3, learning_rate=0.5, step=1e-4, output=None):
    # compute derivatives
    fg0_fun = jax.value_and_grad(model)
    g0_fun = jax.grad(model)
    h0_fun = jax.hessian(model)

    # generate functions
    fg_fun = jax.jit(fg0_fun)
    g_fun = jax.jit(g0_fun)
    h_fun = jax.jit(h0_fun)

    # construct dataset
    N, K = len(y), len(params0)
    data = DataLoader(y, x, batch_size)

    # initialize params
    params = params0.copy()

    # do training
    for ep in range(epochs):
        # epoch stats
        agg_loss, agg_batch = 0.0, 0

        # iterate over batches
        for y_bat, x_bat in data:
            # compute gradients
            loss, diff = fg_fun(params, y_bat, x_bat)

            # compute step
            step = -learning_rate*diff
            params += step

            # error
            gain = np.dot(step, diff)
            move = np.max(np.abs(gain))

            # compute statistics
            agg_loss += loss
            agg_batch += 1

        # display stats
        avg_loss = agg_loss/agg_batch
        print(f'{ep:3}: loss = {avg_loss}')

    # return to device
    if output == 'beta':
        return params.copy(), None

    try:
        # get hessian matrix
        hess = np.zeros((K, K))
        for y_bat, x_bat in data:
            hess += h_fun(params, y_bat, x_bat)
        hess *= batch_size/N
    except Exception as e:
        # our gods have failed us
        print(e) # source of error
        print('Falling back to finite difference for hessian')
        hess_rows = [np.zeros(K) for i in range(K)]
        diff = step*np.eye(K)
        for y_bat, x_bat in data:
            g0_batch = g_fun(params, y_bat, x_bat)[None, :]
            for i in range(K):
                params1 = params + diff[i, :]
                hess_rows[i] += g_fun(params1, y_bat, x_bat) - g0_batch
        hess = np.vstack(hess_rows)*(batch_size/N)/step

    # get cov matrix
    sigma = np.linalg.inv(hess)/N

    # return all
    return params.copy(), sigma.copy()

# default glm specification
def glm(y, x=[], fe=[], data=None, extra=None, link=None, loss=None, intercept=True, drop='first', output=None, table=True, **kwargs):
    # construct design matrices
    y_vec, x_mat, x_names = design_matrices(y, x=x, fe=fe, data=data, intercept=intercept, drop=drop)
    N, K = x_mat.shape

    # pass params to loss function?
    if extra is None:
        loss1 = lambda p, yh, y: loss(yh, y)
        extra = []
    else:
        loss1 = loss

    # account for extra params
    P = len(extra)
    x_names = extra + x_names

    # evaluator
    def model(par, y, x):
        linear = np.dot(x, par[-K:])
        pred = link(linear)
        like = loss1(par, pred, y)
        return -np.mean(like)

    # estimate model
    params = np.zeros(P+K)
    beta, sigma = maxlike(y_vec, x_mat, model, params, output=output, **kwargs)

    # return relevant
    if output == 'beta':
        return beta
    elif table:
        return param_table(beta, sigma, x_names)
    else:
        return beta, sigma

def logit(y, x=[], fe=[], data=None, **kwargs):
    link = links['logit']
    like = losses['binary']
    return glm(y, x=x, fe=fe, data=data, link=link, loss=like, **kwargs)

# poisson regression
def poisson(y, x=[], fe=[], data=None, **kwargs):
    link = links['exponential']
    like = losses['poisson']
    return glm(y, x=x, fe=fe, data=data, link=link, loss=like, **kwargs)

# zero inflated poisson regression
def zero_inflated_poisson(y, x=[], fe=[], data=None, **kwargs):
    # base poisson distribution
    link = links['exponential']
    like0 = losses['poisson']
    extra = ['lpzero']

    # zero inflation
    def loss(par, yh, y):
        pzero = sigmoid(par[0])
        clike = np.clip(like0(yh, y), a_max=clip_like)
        like = pzero*(y==0) + (1-pzero)*np.exp(clike)
        return np.log(like)

    return glm(y, x=x, fe=fe, data=data, extra=extra, link=link, loss=loss, **kwargs)

# negative binomial regression (no standard errors right now)
def negative_binomial(y, x=[], fe=[], data=None, **kwargs):
    link = links['exponential']
    like = losses['negative_binomial']
    extra = ['lalpha']

    def loss(par, yh, y):
        r = np.exp(-par[0])
        return like(r, yh, y)

    return glm(y, x=x, fe=fe, data=data, extra=extra, link=link, loss=loss, **kwargs)

# zero inflated poisson regression
def zero_inflated_negative_binomial(y, x=[], fe=[], data=None, **kwargs):
    # base poisson distribution
    link = links['exponential']
    like0 = losses['negative_binomial']
    extra = ['lpzero', 'lalpha']

    # zero inflation
    def loss(par, yh, y):
        pzero = sigmoid(par[0])
        r = np.exp(-par[1])
        clike = np.clip(like0(r, yh, y), a_max=clip_like)
        like = pzero*(y==0) + (1-pzero)*np.exp(clike)
        return np.log(like)

    return glm(y, x=x, fe=fe, data=data, extra=extra, link=link, loss=loss, **kwargs)

# ordinary least squares (just for kicks)
def ordinary_least_squares(y, x=[], fe=[], data=None, **kwargs):
    # base poisson distribution
    link = links['identity']
    loss0 = losses['least_squares']
    extra = ['lsigma']

    # zero inflation
    def loss(par, yh, y):
        lsigma = par[0]
        sigma2 = np.exp(2*lsigma)
        like = -lsigma + 0.5*loss0(yh, y)/sigma2
        return like

    return glm(y, x=x, fe=fe, data=data, extra=extra, link=link, loss=loss, **kwargs)
